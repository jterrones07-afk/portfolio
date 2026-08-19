#!/usr/bin/env python3
"""Rule-based IMAP inbox cleaner.

This bot connects to an IMAP mailbox, scans recent inbox messages, and applies
safe cleanup actions from a JSON rules file. It defaults to dry-run mode so you
can review proposed actions before anything is changed.
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
from dataclasses import dataclass
from email.message import Message
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

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Rule":
        return cls(
            name=str(data["name"]),
            action=str(data["action"]),  # type: ignore[arg-type]
            destination=data.get("destination") and str(data["destination"]),
            from_contains=_string_list(data.get("from_contains")),
            subject_contains=_string_list(data.get("subject_contains")),
            body_contains=_string_list(data.get("body_contains")),
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
    if message.is_multipart():
        parts: Iterable[Message] = message.walk()
    else:
        parts = [message]

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


def rule_matches(rule: Rule, message: Message) -> bool:
    sender = message.get("from", "")
    subject = message.get("subject", "")
    body = message_text(message)
    return (
        contains_any(sender, rule.from_contains)
        and contains_any(subject, rule.subject_contains)
        and contains_any(body, rule.body_contains)
    )


def connect() -> imaplib.IMAP4_SSL:
    host = os.environ["IMAP_HOST"]
    username = os.environ["IMAP_USERNAME"]
    password = os.environ["IMAP_PASSWORD"]
    mailbox = imaplib.IMAP4_SSL(host)
    mailbox.login(username, password)
    return mailbox


def ensure_destination(rule: Rule) -> str:
    if rule.action == "move" and not rule.destination:
        raise ValueError(f"Rule '{rule.name}' uses action 'move' but has no destination.")
    return rule.destination or ""


def apply_action(mailbox: imaplib.IMAP4_SSL, message_id: bytes, rule: Rule, dry_run: bool) -> None:
    destination = ensure_destination(rule)
    if dry_run:
        return
    if rule.action == "archive":
        archive_mailbox = os.getenv("IMAP_ARCHIVE_MAILBOX", "Archive")
        mailbox.copy(message_id, archive_mailbox)
        mailbox.store(message_id, "+FLAGS", "\\Deleted")
    elif rule.action == "delete":
        mailbox.store(message_id, "+FLAGS", "\\Deleted")
    elif rule.action == "mark_read":
        mailbox.store(message_id, "+FLAGS", "\\Seen")
    elif rule.action == "move":
        mailbox.copy(message_id, destination)
        mailbox.store(message_id, "+FLAGS", "\\Deleted")
    else:
        raise ValueError(f"Unsupported action: {rule.action}")


def clean_inbox(rules: list[Rule], limit: int, dry_run: bool) -> int:
    processed = 0
    with connect() as mailbox:
        mailbox.select("INBOX")
        _, data = mailbox.search(None, "ALL")
        message_ids = data[0].split()[-limit:]
        for message_id in message_ids:
            _, message_data = mailbox.fetch(message_id, "(RFC822)")
            raw_message = message_data[0][1]
            message = email.message_from_bytes(raw_message)
            for rule in rules:
                if rule_matches(rule, message):
                    subject = re.sub(r"\s+", " ", message.get("subject", "(no subject)")).strip()
                    print(f"{message_id.decode()}: {rule.action.upper()} via '{rule.name}' — {subject}")
                    apply_action(mailbox, message_id, rule, dry_run)
                    processed += 1
                    break
        if not dry_run:
            mailbox.expunge()
    return processed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean an IMAP inbox using JSON rules.")
    parser.add_argument("--rules", default="inbox_rules.example.json", help="Path to JSON rules file.")
    parser.add_argument("--limit", type=int, default=50, help="Maximum recent messages to scan.")
    parser.add_argument("--apply", action="store_true", help="Apply changes instead of running a dry run.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rules = load_rules(args.rules)
    count = clean_inbox(rules, args.limit, dry_run=not args.apply)
    mode = "applied" if args.apply else "dry-run matched"
    print(f"Done: {mode} {count} message(s).")


if __name__ == "__main__":
    main()
