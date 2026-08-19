#!/usr/bin/env python3
"""Fast, rule-based IMAP inbox organizer with safe dry-run by default."""

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
from typing import Iterable, Literal

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
            name=str(data["name"]),
            action=action,  # type: ignore[arg-type]
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
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Rule match fields must be lists of strings.")
    return value


def load_rules(path: str) -> list[Rule]:
    with open(path, "r", encoding="utf-8") as rules_file:
        raw_rules = json.load(rules_file)
    if not isinstance(raw_rules, list):
        raise ValueError("Rules file must contain a JSON array.")
    return [Rule.from_dict(rule) for rule in raw_rules]


def message_text(message: Message) -> str:
    parts: Iterable[Message] = message.walk() if message.is_multipart() else [message]
    content: list[str] = []
    for part in parts:
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        content.append(payload.decode(charset, errors="replace"))
    return "\n".join(content)


def contains_any(haystack: str, needles: list[str] | None) -> bool:
    if not needles:
        return True
    normalized = haystack.casefold()
    return any(needle.casefold() in normalized for needle in needles)


def decoded_subject(message: Message) -> str:
    return str(make_header(decode_header(message.get("subject", ""))))


def sender_text(message: Message) -> str:
    raw_sender = message.get("from", "")
    display_name, address = parseaddr(raw_sender)
    return " ".join((raw_sender, display_name, address)).casefold()


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

    needs_body = bool(rule.body_contains or rule.exclude_body_contains)
    if needs_body:
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
        raise RuntimeError("Set IMAP_HOST, IMAP_USERNAME, and IMAP_PASSWORD before running.")
    mailbox = imaplib.IMAP4_SSL(host, timeout=60)
    mailbox.login(username, password)
    return mailbox


def ensure_destination(mailbox: imaplib.IMAP4_SSL, rule: Rule, dry_run: bool) -> str:
    if rule.action == "move" and not rule.destination:
        raise ValueError(f"Rule '{rule.name}' uses action 'move' but has no destination.")
    destination = rule.destination or ""
    if rule.action != "move" or dry_run:
        return destination

    status, mailboxes = mailbox.list(pattern=f'"{destination}"')
    if status == "OK" and mailboxes:
        return destination

    status, _ = mailbox.create(destination)
    if status != "OK":
        raise RuntimeError(
            f"Destination mailbox/label '{destination}' does not exist and could not be created."
        )
    return destination


def copy_and_delete(mailbox: imaplib.IMAP4_SSL, message_id: bytes, destination: str) -> None:
    status, _ = mailbox.copy(message_id, destination)
    if status != "OK":
        raise RuntimeError(
            f"IMAP COPY failed for message {message_id.decode()}; original was not deleted."
        )
    status, _ = mailbox.store(message_id, "+FLAGS", "\\Deleted")
    if status != "OK":
        raise RuntimeError(
            f"IMAP STORE failed after copying message {message_id.decode()} to '{destination}'."
        )


def apply_action(
    mailbox: imaplib.IMAP4_SSL,
    message_id: bytes,
    rule: Rule,
    dry_run: bool,
    destinations: dict[str, str],
) -> None:
    if dry_run:
        return
    if rule.action == "archive":
        copy_and_delete(mailbox, message_id, os.getenv("IMAP_ARCHIVE_MAILBOX", "Archive"))
    elif rule.action == "delete":
        status, _ = mailbox.store(message_id, "+FLAGS", "\\Deleted")
        if status != "OK":
            raise RuntimeError(f"IMAP STORE failed while deleting {message_id.decode()}.")
    elif rule.action == "mark_read":
        status, _ = mailbox.store(message_id, "+FLAGS", "\\Seen")
        if status != "OK":
            raise RuntimeError(f"IMAP STORE failed while marking {message_id.decode()} read.")
    elif rule.action == "move":
        copy_and_delete(mailbox, message_id, destinations[rule.name])
    else:
        raise ValueError(f"Unsupported action: {rule.action}")


def fetch_headers_batch(
    mailbox: imaplib.IMAP4_SSL, message_ids: list[bytes]
) -> dict[bytes, Message]:
    if not message_ids:
        return {}
    message_set = ",".join(item.decode() for item in message_ids)
    status, data = mailbox.fetch(message_set, "(BODY.PEEK[HEADER])")
    if status != "OK":
        raise RuntimeError("IMAP batch header fetch failed.")

    result: dict[bytes, Message] = {}
    for item in data:
        if not isinstance(item, tuple):
            continue
        match = re.match(rb"(\d+) \(BODY\.PEEK\[HEADER\]", item[0])
        if match:
            result[match.group(1)] = email.message_from_bytes(item[1])
    return result


def fetch_body(mailbox: imaplib.IMAP4_SSL, message_id: bytes) -> Message | None:
    status, data = mailbox.fetch(message_id, "(BODY.PEEK[])")
    if status != "OK" or not data or not isinstance(data[0], tuple):
        return None
    return email.message_from_bytes(data[0][1])


def rule_needs_body(rule: Rule) -> bool:
    return bool(rule.body_contains or rule.exclude_body_contains)


def evaluate_message(
    mailbox: imaplib.IMAP4_SSL,
    message_id: bytes,
    header: Message,
    rules: list[Rule],
) -> Rule | None:
    body_rules = [rule for rule in rules if rule_needs_body(rule)]

    # Fast path: most rules only need sender/subject headers.
    for rule in rules:
        if not rule_needs_body(rule) and rule_matches(rule, header):
            return rule

    # Only download a full email when a rule explicitly needs its body.
    if body_rules:
        full_message = fetch_body(mailbox, message_id)
        if full_message is None:
            print(f"{message_id.decode()}: skipped because IMAP BODY FETCH failed")
            return None
        body = message_text(full_message)
        for rule in body_rules:
            if rule_matches(rule, full_message, body):
                return rule
    return None


def get_message_ids(
    mailbox: imaplib.IMAP4_SSL,
    limit: int | None,
    since_days: int | None,
) -> list[bytes]:
    if since_days is not None:
        if since_days < 0:
            raise ValueError("--since-days cannot be negative.")
        since_date = time.strftime(
            "%d-%b-%Y", time.localtime(time.time() - since_days * 86400)
        )
        status, data = mailbox.search(None, "SINCE", since_date)
    else:
        status, data = mailbox.search(None, "ALL")
    if status != "OK":
        raise RuntimeError("Unable to search INBOX.")

    message_ids = data[0].split()
    if limit is not None:
        message_ids = message_ids[-limit:]
    return message_ids


def clean_inbox(
    rules: list[Rule],
    limit: int | None,
    dry_run: bool,
    batch_size: int,
    since_days: int | None,
    progress_every: int,
    continue_on_error: bool,
) -> int:
    if limit is not None and limit < 1:
        raise ValueError("--limit must be a positive integer.")
    if batch_size < 1:
        raise ValueError("--batch-size must be positive.")

    processed = 0
    matched = 0
    errors = 0
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
                    destinations[rule.name] = ensure_destination(mailbox, rule, dry_run=False)

        for start in range(0, len(message_ids), batch_size):
            batch = message_ids[start : start + batch_size]
            headers = fetch_headers_batch(mailbox, batch)

            for message_id in batch:
                processed += 1
                header = headers.get(message_id)
                if header is None:
                    print(f"{message_id.decode()}: skipped because header was not returned")
                    errors += 1
                    continue

                rule = evaluate_message(mailbox, message_id, header, rules)
                if rule is None:
                    continue

                subject = re.sub(r"\s+", " ", decoded_subject(header) or "(no subject)").strip()
                print(
                    f"{message_id.decode()}: {rule.action.upper()} via "
                    f"'{rule.name}' — {subject}"
                )
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
                rate = processed / elapsed
                print(
                    f"Progress: {processed:,}/{len(message_ids):,} "
                    f"({rate:.1f} msg/s), matched {matched:,}, errors {errors:,}"
                )

        if not dry_run and matched:
            mailbox.expunge()

    print(
        f"Finished: scanned {processed:,}, matched {matched:,}, "
        f"errors {errors:,}."
    )
    return matched


def analyze_inbox(limit: int | None, batch_size: int) -> None:
    if limit is not None and limit < 1:
        raise ValueError("--limit must be a positive integer.")
    with connect() as mailbox:
        status, _ = mailbox.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("Unable to select INBOX.")
        message_ids = get_message_ids(mailbox, limit, None)
        print(f"Analyzing {len(message_ids):,} message(s)...")
        for start in range(0, len(message_ids), batch_size):
            batch = message_ids[start : start + batch_size]
            headers = fetch_headers_batch(mailbox, batch)
            for message_id in batch:
                message = headers.get(message_id)
                if message is None:
                    continue
                sender = message.get("from", "(unknown sender)")
                subject = decoded_subject(message) or "(no subject)"
                print(f"{message_id.decode()} | {sender} | {subject}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fast IMAP inbox organizer. Dry-run is the default."
    )
    parser.add_argument("--rules", default="inbox_rules.example.json")
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum recent messages to scan. Omit with --all.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scan every message currently in INBOX.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of message headers to fetch per IMAP request.",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        help="Only scan messages received within the last N days.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually apply rule actions.",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="List recent sender/subject headers without applying rules.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Log individual IMAP errors and continue instead of stopping.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=250,
        help="Print progress after this many messages.",
    )
    args = parser.parse_args()
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
    count = clean_inbox(
        rules,
        args.limit,
        dry_run=not args.apply,
        batch_size=args.batch_size,
        since_days=args.since_days,
        progress_every=args.progress_every,
        continue_on_error=args.continue_on_error,
    )
    mode = "applied" if args.apply else "dry-run matched"
    print(f"Done: {mode} {count:,} message(s).")


if __name__ == "__main__":
    main()
