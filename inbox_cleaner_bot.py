#!/usr/bin/env python3
"""Rule-based IMAP inbox cleaner with safe dry-run and analysis modes."""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
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
        return cls(
            name=str(data["name"]),
            action=str(data["action"]),  # type: ignore[arg-type]
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
    normalized = haystack.lower()
    return any(needle.lower() in normalized for needle in needles)


def sender_text(message: Message) -> str:
    raw_sender = message.get("from", "")
    display_name, address = parseaddr(raw_sender)
    return f"{raw_sender} {display_name} {address}".lower()


def rule_matches(rule: Rule, message: Message, body: str | None = None) -> bool:
    sender = sender_text(message)
    subject = str(make_header(decode_header(message.get("subject", ""))))
    if not contains_any(sender, rule.from_contains):
        return False
    if not contains_any(subject, rule.subject_contains):
        return False
    if contains_any(sender, rule.exclude_from_contains):
        return False
    if contains_any(subject, rule.exclude_subject_contains):
        return False
    needs_body = bool(rule.body_contains or rule.exclude_body_contains)
    if needs_body:
        body = body if body is not None else message_text(message)
        if not contains_any(body, rule.body_contains):
            return False
        if contains_any(body, rule.exclude_body_contains):
            return False
    return True


def connect() -> imaplib.IMAP4_SSL:
    mailbox = imaplib.IMAP4_SSL(os.environ["IMAP_HOST"], timeout=30)
    mailbox.login(os.environ["IMAP_USERNAME"], os.environ["IMAP_PASSWORD"])
    return mailbox


def ensure_destination(rule: Rule) -> str:
    if rule.action == "move" and not rule.destination:
        raise ValueError(f"Rule '{rule.name}' uses action 'move' but has no destination.")
    return rule.destination or ""


def copy_and_delete(mailbox: imaplib.IMAP4_SSL, message_id: bytes, destination: str) -> None:
    status, _ = mailbox.copy(message_id, destination)
    if status != "OK":
        raise RuntimeError(
            f"IMAP COPY failed for message {message_id.decode()} to mailbox '{destination}'; "
            "original message was not deleted."
        )
    status, _ = mailbox.store(message_id, "+FLAGS", "\\Deleted")
    if status != "OK":
        raise RuntimeError(
            f"IMAP STORE failed while marking message {message_id.decode()} as deleted "
            f"after a successful copy to '{destination}'."
        )


def apply_action(mailbox: imaplib.IMAP4_SSL, message_id: bytes, rule: Rule, dry_run: bool) -> None:
    destination = ensure_destination(rule)
    if dry_run:
        return
    if rule.action == "archive":
        copy_and_delete(mailbox, message_id, os.getenv("IMAP_ARCHIVE_MAILBOX", "Archive"))
    elif rule.action == "delete":
        status, _ = mailbox.store(message_id, "+FLAGS", "\\Deleted")
        if status != "OK":
            raise RuntimeError(f"IMAP STORE failed while deleting message {message_id.decode()}.")
    elif rule.action == "mark_read":
        status, _ = mailbox.store(message_id, "+FLAGS", "\\Seen")
        if status != "OK":
            raise RuntimeError(f"IMAP STORE failed while marking message {message_id.decode()} as read.")
    elif rule.action == "move":
        copy_and_delete(mailbox, message_id, destination)
    else:
        raise ValueError(f"Unsupported action: {rule.action}")


def fetch_header(mailbox: imaplib.IMAP4_SSL, message_id: bytes) -> Message | None:
    status, data = mailbox.fetch(message_id, "(BODY.PEEK[HEADER])")
    if status != "OK" or not data or not isinstance(data[0], tuple):
        return None
    return email.message_from_bytes(data[0][1])


def fetch_body(mailbox: imaplib.IMAP4_SSL, message_id: bytes) -> Message | None:
    status, data = mailbox.fetch(message_id, "(BODY.PEEK[])")
    if status != "OK" or not data or not isinstance(data[0], tuple):
        return None
    return email.message_from_bytes(data[0][1])


def rule_needs_body(rule: Rule) -> bool:
    return bool(rule.body_contains or rule.exclude_body_contains)


def evaluate_message(mailbox: imaplib.IMAP4_SSL, message_id: bytes, rules: list[Rule]) -> tuple[Message | None, Rule | None]:
    header = fetch_header(mailbox, message_id)
    if header is None:
        print(f"{message_id.decode()}: skipped because IMAP HEADER FETCH failed")
        return None, None

    full_message: Message | None = None
    body: str | None = None
    for rule in rules:
        if rule_needs_body(rule):
            if full_message is None:
                full_message = fetch_body(mailbox, message_id)
                if full_message is None:
                    print(f"{message_id.decode()}: skipped because IMAP BODY FETCH failed")
                    return header, None
                body = message_text(full_message)
            if rule_matches(rule, full_message, body):
                return full_message, rule
        elif rule_matches(rule, header):
            return header, rule
    return header, None


def clean_inbox(rules: list[Rule], limit: int, dry_run: bool) -> int:
    if limit < 1:
        raise ValueError("--limit must be a positive integer.")
    processed = 0
    with connect() as mailbox:
        status, _ = mailbox.select("INBOX")
        if status != "OK":
            raise RuntimeError("Unable to select INBOX.")
        status, data = mailbox.search(None, "ALL")
        if status != "OK":
            raise RuntimeError("Unable to search INBOX.")
        message_ids = data[0].split()[-limit:]
        for message_id in message_ids:
            message, rule = evaluate_message(mailbox, message_id, rules)
            if rule and message:
                subject = re.sub(r"\s+", " ", str(make_header(decode_header(message.get("subject", "(no subject)"))))).strip()
                print(f"{message_id.decode()}: {rule.action.upper()} via '{rule.name}' — {subject}")
                apply_action(mailbox, message_id, rule, dry_run)
                processed += 1
        if not dry_run:
            mailbox.expunge()
    return processed


def analyze_inbox(limit: int) -> None:
    if limit < 1:
        raise ValueError("--limit must be a positive integer.")
    with connect() as mailbox:
        status, _ = mailbox.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("Unable to select INBOX.")
        status, data = mailbox.search(None, "ALL")
        if status != "OK":
            raise RuntimeError("Unable to search INBOX.")
        message_ids = data[0].split()[-limit:]
        print(f"Analyzing {len(message_ids)} message(s)...")
        for message_id in message_ids:
            message = fetch_header(mailbox, message_id)
            if message is None:
                continue
            sender = message.get("from", "(unknown sender)")
            subject = str(make_header(decode_header(message.get("subject", "(no subject)"))))
            print(f"{message_id.decode()} | {sender} | {subject}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean or analyze an IMAP inbox using JSON rules.")
    parser.add_argument("--rules", default="inbox_rules.example.json", help="Path to JSON rules file.")
    parser.add_argument("--limit", type=int, default=50, help="Maximum recent messages to scan.")
    parser.add_argument("--apply", action="store_true", help="Apply changes instead of running a dry run.")
    parser.add_argument("--analyze", action="store_true", help="List recent sender/subject headers without applying rules.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.analyze:
        analyze_inbox(args.limit)
        return
    rules = load_rules(args.rules)
    count = clean_inbox(rules, args.limit, dry_run=not args.apply)
    mode = "applied" if args.apply else "dry-run matched"
    print(f"Done: {mode} {count} message(s).")


if __name__ == "__main__":
    main()
