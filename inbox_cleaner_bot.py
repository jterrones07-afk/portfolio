#!/usr/bin/env python3
"""Fast, rule-based IMAP inbox organizer. Dry-run by default."""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
import time
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from typing import Literal

Action = Literal["archive", "delete", "move", "mark_read"]

@dataclass(frozen=True)
class Rule:
    name: str
    action: Action
    destination: str | None = None
    from_contains: list[str] | None = None
    subject_contains: list[str] | None = None
    body_contains: list[str] | None = None
    exclude_from_contains: list[str] | None = None
    exclude_subject_contains: list[str] | None = None
    exclude_body_contains: list[str] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Rule":
        action = str(data["action"])
        if action not in {"archive", "delete", "move", "mark_read"}:
            raise ValueError(f"Unsupported action: {action}")
        return cls(
            name=str(data["name"]), action=action,  # type: ignore[arg-type]
            destination=data.get("destination") and str(data["destination"]),
            from_contains=_string_list(data.get("from_contains")),
            subject_contains=_string_list(data.get("subject_contains")),
            body_contains=_string_list(data.get("body_contains")),
            exclude_from_contains=_string_list(data.get("exclude_from_contains")),
            exclude_subject_contains=_string_list(data.get("exclude_subject_contains")),
            exclude_body_contains=_string_list(data.get("exclude_body_contains")),
        )

def _string_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError("Rule match fields must be lists of strings.")
    return value

def load_rules(path: str) -> list[Rule]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise ValueError("Rules file must contain a JSON array.")
    return [Rule.from_dict(x) for x in raw]

def decoded_subject(message: Message) -> str:
    return str(make_header(decode_header(message.get("subject", ""))))

def sender_text(message: Message) -> str:
    raw = message.get("from", "")
    name, address = parseaddr(raw)
    return " ".join((raw, name, address)).casefold()

def contains_any(text: str, needles: list[str] | None) -> bool:
    if not needles:
        return True
    text = text.casefold()
    return any(n.casefold() in text for n in needles)

def message_text(message: Message) -> str:
    parts = message.walk() if message.is_multipart() else [message]
    chunks: list[str] = []
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        chunks.append(payload.decode(charset, errors="replace"))
    return "\n".join(chunks)

def rule_needs_body(rule: Rule) -> bool:
    return bool(rule.body_contains or rule.exclude_body_contains)

def rule_matches(rule: Rule, message: Message, body: str | None = None) -> bool:
    sender = sender_text(message)
    subject = decoded_subject(message).casefold()
    if rule.from_contains and not contains_any(sender, rule.from_contains):
        return False
    if rule.subject_contains and not contains_any(subject, rule.subject_contains):
        return False
    if rule.exclude_from_contains and contains_any(sender, rule.exclude_from_contains):
        return False
    if rule.exclude_subject_contains and contains_any(subject, rule.exclude_subject_contains):
        return False
    if rule_needs_body(rule):
        body = body if body is not None else message_text(message)
        if rule.body_contains and not contains_any(body, rule.body_contains):
            return False
        if rule.exclude_body_contains and contains_any(body, rule.exclude_body_contains):
            return False
    return True

def connect() -> imaplib.IMAP4_SSL:
    host = os.environ.get("IMAP_HOST")
    username = os.environ.get("IMAP_USERNAME")
    password = os.environ.get("IMAP_PASSWORD")
    if not host or not username or not password:
        raise RuntimeError("Set IMAP_HOST, IMAP_USERNAME, and IMAP_PASSWORD first.")
    mailbox = imaplib.IMAP4_SSL(host, timeout=60)
    mailbox.login(username, password)
    return mailbox

def ensure_destination(mailbox: imaplib.IMAP4_SSL, rule: Rule, dry_run: bool) -> str:
    if rule.action == "move" and not rule.destination:
        raise ValueError(f"Rule '{rule.name}' has no destination.")
    destination = rule.destination or ""
    if rule.action != "move" or dry_run:
        return destination
    status, mailboxes = mailbox.list(pattern=f'"{destination}"')
    if status == "OK" and mailboxes:
        return destination
    status, _ = mailbox.create(destination)
    if status != "OK":
        raise RuntimeError(f"Could not create destination '{destination}'.")
    return destination

def copy_and_delete(mailbox: imaplib.IMAP4_SSL, message_id: bytes, destination: str) -> None:
    status, _ = mailbox.copy(message_id, destination)
    if status != "OK":
        raise RuntimeError(f"COPY failed for {message_id.decode()}; original was not deleted.")
    status, _ = mailbox.store(message_id, "+FLAGS", r"\Deleted")
    if status != "OK":
        raise RuntimeError(f"STORE failed after copying {message_id.decode()} to '{destination}'.")

def apply_action(mailbox: imaplib.IMAP4_SSL, message_id: bytes, rule: Rule, dry_run: bool, destinations: dict[str, str]) -> None:
    if dry_run:
        return
    if rule.action == "archive":
        copy_and_delete(mailbox, message_id, os.getenv("IMAP_ARCHIVE_MAILBOX", "Archive"))
    elif rule.action == "delete":
        status, _ = mailbox.store(message_id, "+FLAGS", r"\Deleted")
        if status != "OK":
            raise RuntimeError(f"DELETE failed for {message_id.decode()}.")
    elif rule.action == "mark_read":
        status, _ = mailbox.store(message_id, "+FLAGS", r"\Seen")
        if status != "OK":
            raise RuntimeError(f"MARK_READ failed for {message_id.decode()}.")
    elif rule.action == "move":
        copy_and_delete(mailbox, message_id, destinations[rule.name])

def fetch_headers_batch(mailbox: imaplib.IMAP4_SSL, message_ids: list[bytes]) -> dict[bytes, Message]:
    """Fetch From/Subject headers; tolerate Gmail BODY[HEADER] response variants."""
    if not message_ids:
        return {}
    message_set = ",".join(x.decode() for x in message_ids)
    status, data = mailbox.fetch(message_set, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
    if status != "OK":
        raise RuntimeError("IMAP batch header fetch failed.")
    result: dict[bytes, Message] = {}
    pattern = re.compile(rb"^(\d+)\s+\(.*?BODY(?:\.PEEK)?\[HEADER(?:\.FIELDS)?", re.I)
    for item in data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        prefix, raw_header = item[0], item[1]
        if isinstance(prefix, bytes) and isinstance(raw_header, bytes):
            match = pattern.match(prefix)
            if match:
                result[match.group(1)] = email.message_from_bytes(raw_header)
    # Fallback for servers that return an unexpected FETCH response shape.
    for message_id in message_ids:
        if message_id in result:
            continue
        status, single = mailbox.fetch(message_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
        if status != "OK":
            continue
        for item in single:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                result[message_id] = email.message_from_bytes(item[1])
                break
    return result

def fetch_body(mailbox: imaplib.IMAP4_SSL, message_id: bytes) -> Message | None:
    status, data = mailbox.fetch(message_id, "(BODY.PEEK[])")
    if status != "OK":
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return email.message_from_bytes(item[1])
    return None

def evaluate_message(mailbox: imaplib.IMAP4_SSL, message_id: bytes, header: Message, rules: list[Rule]) -> Rule | None:
    for rule in rules:
        if not rule_needs_body(rule) and rule_matches(rule, header):
            return rule
    body_rules = [r for r in rules if rule_needs_body(r)]
    if not body_rules:
        return None
    full_message = fetch_body(mailbox, message_id)
    if full_message is None:
        return None
    body = message_text(full_message)
    for rule in body_rules:
        if rule_matches(rule, full_message, body):
            return rule
    return None

def get_message_ids(mailbox: imaplib.IMAP4_SSL, limit: int | None, since_days: int | None) -> list[bytes]:
    if since_days is not None:
        if since_days < 0:
            raise ValueError("--since-days cannot be negative.")
        date = time.strftime("%d-%b-%Y", time.localtime(time.time() - since_days * 86400))
        status, data = mailbox.search(None, "SINCE", date)
    else:
        status, data = mailbox.search(None, "ALL")
    if status != "OK":
        raise RuntimeError("Unable to search INBOX.")
    ids = data[0].split()
    if limit is not None:
        ids = ids[-limit:]
    return ids

def clean_inbox(rules: list[Rule], limit: int | None, dry_run: bool, batch_size: int, since_days: int | None, progress_every: int, continue_on_error: bool) -> int:
    if limit is not None and limit < 1:
        raise ValueError("--limit must be positive.")
    if batch_size < 1:
        raise ValueError("--batch-size must be positive.")
    processed = matched = errors = 0
    started = time.time()
    with connect() as mailbox:
        status, _ = mailbox.select("INBOX")
        if status != "OK":
            raise RuntimeError("Unable to select INBOX.")
        message_ids = get_message_ids(mailbox, limit, since_days)
        print(f"Scanning {len(message_ids):,} message(s) in batches of {batch_size}...")
        destinations: dict[str, str] = {}
        if not dry_run:
            for rule in rules:
                if rule.action == "move":
                    destinations[rule.name] = ensure_destination(mailbox, rule, False)
        for start in range(0, len(message_ids), batch_size):
            batch = message_ids[start:start + batch_size]
            headers = fetch_headers_batch(mailbox, batch)
            for message_id in batch:
                processed += 1
                header = headers.get(message_id)
                if header is None:
                    errors += 1
                    print(f"{message_id.decode()}: header unavailable; skipped")
                    continue
                rule = evaluate_message(mailbox, message_id, header, rules)
                if rule is None:
                    continue
                subject = re.sub(r"\s+", " ", decoded_subject(header) or "(no subject)").strip()
                print(f"{message_id.decode()}: {rule.action.upper()} via '{rule.name}' — {subject}")
                matched += 1
                try:
                    apply_action(mailbox, message_id, rule, dry_run, destinations)
                except Exception as exc:
                    errors += 1
                    print(f"  ERROR: {exc}")
                    if not continue_on_error:
                        raise
            if processed % progress_every == 0 or processed == len(message_ids):
                elapsed = max(time.time() - started, 0.001)
                print(f"Progress: {processed:,}/{len(message_ids):,} ({processed / elapsed:.1f} msg/s), matched {matched:,}, errors {errors:,}")
        if not dry_run and matched:
            mailbox.expunge()
    print(f"Finished: scanned {processed:,}, matched {matched:,}, errors {errors:,}.")
    return matched

def analyze_inbox(limit: int | None, batch_size: int) -> None:
    with connect() as mailbox:
        status, _ = mailbox.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("Unable to select INBOX.")
        ids = get_message_ids(mailbox, limit, None)
        print(f"Analyzing {len(ids):,} message(s)...")
        for start in range(0, len(ids), batch_size):
            batch = ids[start:start + batch_size]
            headers = fetch_headers_batch(mailbox, batch)
            for mid in batch:
                msg = headers.get(mid)
                if msg is not None:
                    print(f"{mid.decode()} | {msg.get('from', '(unknown)')} | {decoded_subject(msg) or '(no subject)'}")

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fast IMAP inbox organizer. Dry-run by default.")
    p.add_argument("--rules", default="inbox_rules.example.json")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--all", action="store_true", help="Scan every message in INBOX.")
    p.add_argument("--batch-size", type=int, default=100)
    p.add_argument("--since-days", type=int)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--analyze", action="store_true")
    p.add_argument("--continue-on-error", action="store_true")
    p.add_argument("--progress-every", type=int, default=250)
    args = p.parse_args()
    if args.all:
        args.limit = None
    if args.progress_every < 1:
        raise ValueError("--progress-every must be positive.")
    return args

def main() -> None:
    args = parse_args()
    if args.analyze:
        analyze_inbox(args.limit, args.batch_size)
        return
    rules = load_rules(args.rules)
    count = clean_inbox(rules, args.limit, dry_run=not args.apply, batch_size=args.batch_size, since_days=args.since_days, progress_every=args.progress_every, continue_on_error=args.continue_on_error)
    print(f"Done: {'applied' if args.apply else 'dry-run matched'} {count:,} message(s).")

if __name__ == "__main__":
    main()
