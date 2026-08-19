# Portfolio

Static portfolio site plus a starter inbox-cleaner bot.

## Inbox cleaner bot

`inbox_cleaner_bot.py` is a safe, rule-based IMAP bot that helps keep an email
inbox clean by archiving, deleting, moving, or marking messages as read. It runs
in dry-run mode by default, so you can review what it would change before it
modifies your mailbox.

### Setup

1. Copy `inbox_rules.example.json` to `inbox_rules.json` and edit the rules for
   your inbox.
2. Set IMAP credentials as environment variables:

   ```bash
   export IMAP_HOST="imap.gmail.com"
   export IMAP_USERNAME="you@example.com"
   export IMAP_PASSWORD="your-app-password"
   export IMAP_ARCHIVE_MAILBOX="Archive" # Optional; defaults to Archive
   ```

   For Gmail, use an app password and make sure IMAP is enabled for the account.

### Run a dry run

```bash
python3 inbox_cleaner_bot.py --rules inbox_rules.json --limit 50
```

### Apply changes

```bash
python3 inbox_cleaner_bot.py --rules inbox_rules.json --limit 50 --apply
```

### Rule format

Each rule can match `from_contains`, `subject_contains`, and `body_contains`.
If a field is omitted, that field is treated as a match. The first matching rule
wins.

Supported actions:

- `archive` moves the message to `IMAP_ARCHIVE_MAILBOX`, which defaults to `Archive`.
- `delete` marks the message for deletion.
- `move` copies the message to `destination` and removes it from the inbox.
- `mark_read` marks the message as read.
