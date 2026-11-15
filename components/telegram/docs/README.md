# Telegram notification component 0.1.0

**State: isolated component; not registered with `core/server.py`, not installed on HOME, and not tested against Telegram.** It sends plain text through Telegram Bot API `sendMessage` only when a future caller invokes it while the MCP process is running. It creates no service, scheduler, polling loop, or background worker.

## Fixed destination

The intended destination labels are group **АП_Лазер** and topic **МСП Биржа облигации**. These names are not Telegram identifiers. The numeric `chat_id` and `message_thread_id` have not been verified, so the configuration template leaves them blank and keeps `MSP_TELEGRAM_TARGET_CONFIRMED=0`.

The component requires a numeric `-100…` forum-supergroup ID, a positive topic ID, and explicit local confirmation. `send_event(event_id, text)` has no recipient parameters: every event uses the one immutable pair loaded for that process. The package does not accept `@username`, chat IDs, or topic IDs from event data. Do not set the confirmation flag until the owner has verified the exact ID pair against the intended group and topic.

Telegram documents `chat_id` and `message_thread_id` as separate `sendMessage` parameters; the latter identifies a forum topic. The Bot API also returns `retry_after` in seconds when flood control is exceeded. See the [Bot API `sendMessage` method](https://core.telegram.org/bots/api#sendmessage) and [ResponseParameters](https://core.telegram.org/bots/api#responseparameters).

## Token and local state

The bot token is never read from environment variables or `.env` files. `WindowsCredentialProvider` accepts only the native `keyring.backends.Windows.WinVaultKeyring` backend and fails closed if another backend is selected. The token is requested from Windows Credential Locker under service `msp-bonds-telegram`, username `bot-token`; it is not stored in this repository, the local delivery ledger, or application logs. Python `keyring` documents Windows Credential Locker and its interactive `set` command; only after separate approval and bot creation would the local setup use `python -m keyring set msp-bonds-telegram bot-token`. Never use the command's `get` form in a shared terminal.

The deduplication ledger is stored by default at `%LOCALAPPDATA%\MSP-Bonds-MCP\telegram\delivery.sqlite3`, outside the checkout. It records only SHA-256 hashes of event IDs and message text, a delivery state, and a retry time; it stores no raw message, token, chat ID, or topic ID. Windows user-folder permissions protect the file. If the ledger cannot be read or written, delivery stops.

## Delivery and failure behavior

- Plain text only; 1–4096 Unicode characters. No parse mode or attachment support.
- HTTPS timeout: 8 seconds. At most 2 attempts per call.
- A 429 is retried once only when Telegram supplies `retry_after` of at most 5 seconds. Longer waits are recorded and returned as deferred; a later caller may retry only after that time. No scheduler is created.
- Definitive 4xx errors, including access denial and invalid topics, are not retried. Network errors, 5xx responses, malformed success responses, and interrupted ledger updates are marked uncertain and block the same event ID, because Telegram may already have accepted the message.
- Reusing an event ID with the same content is suppressed after a send or uncertain result. Reusing it with different content is blocked. After an uncertain result, investigate before creating a new event ID.
- `notify_best_effort` returns a small status instead of raising delivery errors, so a future primary MCP task can continue. It does not print, log, or expose exception text. Calls are synchronous and bounded; if the stdio server is stopped, no notification is produced.

The HTTP transport disables redirects because the Bot API URL contains the token in its path. The application never logs that URL or raw Telegram response bodies.

## Offline tests

From the repository root, with the package source available on `PYTHONPATH`:

```powershell
python -B -m unittest discover -s components/telegram/tests -p 'test_*.py' -v
```

Tests use fake credentials, a fake HTTP transport, and temporary SQLite files. They do not create a bot, read a real token, or contact Telegram.

## Future integration boundary

This package is intentionally separate from `msp_bonds_google`, `msp_bonds_vtb_reports`, and the shared MCP server. A later integration proposal must separately decide which events may notify, how the caller creates stable event IDs, and whether the notification result is surfaced to the caller. It must preserve the 16 existing MCP tool names and schemas, catch failures through `notify_best_effort`, and must not add a background service. No event policy, user data fields, or notification schedule is approved by this package.
