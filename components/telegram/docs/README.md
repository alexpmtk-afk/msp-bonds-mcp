# Telegram notification component 0.1.0

**State (18.11.2026; based on prior HOME checks):** Telegram 0.1.0 is present in the HOME source tree, included in `scripts/install-local.ps1`, and registered as a separate `send_telegram_notification(event_id, text)` tool in `core/server.py`. Earlier HOME checks confirmed installed editable-package metadata and an 18-tool stdio contract (17 pre-existing HOME schemas plus Telegram). The latest stdio diagnostic failed during Windows stdin-pipe creation, before the child MCP process started (`WinError 5`; cause unknown); current runtime availability remains unverified. Current destination settings, Credential Locker availability, and delivery through the installed MCP tool have not been verified. The owner previously confirmed a separate smoke-test delivery through the component source; this did not verify the installed MCP tool. No service, scheduler, polling loop, or background worker is created. Public `main` registers 17 tools (16 original tools plus Telegram), distinct from the earlier HOME inventory.

## Fixed destination

The intended destination labels are group **АП_Лазер** and topic **МСП Биржа облигации**. The owner-provided topic link and successful smoke test confirm the destination. The real numeric `chat_id` and `message_thread_id` are intentionally kept out of this public repository; the configuration template leaves them blank and keeps `MSP_TELEGRAM_TARGET_CONFIRMED=0`.

The component requires a numeric `-100…` forum-supergroup ID, a positive topic ID, and explicit local confirmation. `send_event(event_id, text)` and the MCP adapter have no recipient parameters: every event uses the one immutable pair loaded for that process. The package does not accept `@username`, chat IDs, or topic IDs from event data. Keep the actual IDs in the local MCP client environment only.

Telegram documents `chat_id` and `message_thread_id` as separate `sendMessage` parameters; the latter identifies a forum topic. The Bot API also returns `retry_after` in seconds when flood control is exceeded. See the [Bot API `sendMessage` method](https://core.telegram.org/bots/api#sendmessage) and [ResponseParameters](https://core.telegram.org/bots/api#responseparameters).

## Token and local state

The bot token is never read from environment variables or `.env` files. `WindowsCredentialProvider` accepts only the native `keyring.backends.Windows.WinVaultKeyring` backend and fails closed if another backend is selected. The token is requested from Windows Credential Locker under service `msp-bonds-telegram`, username `bot-token`; it is not stored in this repository, the local delivery ledger, or application logs. On Windows, save the full token through the hidden prompt with `py -3.13 -m keyring set msp-bonds-telegram bot-token`. Never use the command's `get` form in a shared terminal.

The deduplication ledger is stored by default at `%LOCALAPPDATA%\MSP-Bonds-MCP\telegram\delivery.sqlite3`, outside the checkout. It records only SHA-256 hashes of event IDs and message text, a delivery state, and a retry time; it stores no raw message, token, chat ID, or topic ID. Windows user-folder permissions protect the file. If the ledger cannot be read or written, delivery stops.

## Delivery and failure behavior

- Plain text only; 1–4096 Unicode characters. No parse mode or attachment support.
- HTTPS timeout: 8 seconds. At most 2 attempts per call.
- A 429 is retried once only when Telegram supplies `retry_after` of at most 5 seconds. Longer waits are recorded and returned as deferred; a later caller may retry only after that time. No scheduler is created.
- Definitive 4xx errors, including access denial and invalid topics, are not retried. Network errors, 5xx responses, malformed success responses, and interrupted ledger updates are marked uncertain and block the same event ID, because Telegram may already have accepted the message.
- Reusing an event ID with the same content is suppressed after a send or uncertain result. Reusing it with different content is blocked. After an uncertain result, investigate before creating a new event ID.
- `notify_best_effort` returns a small status instead of raising delivery errors, so a future primary MCP task can continue. It does not print, log, or expose exception text. Calls are synchronous and bounded; if the stdio server is stopped, no notification is produced.

The HTTP transport disables redirects because the Bot API URL contains the token in its path. The application never logs that URL or raw Telegram response bodies.

## MCP adapter

`send_telegram_notification(event_id, text)` is a separate explicit MCP operation. It has no recipient arguments and uses the one locally configured destination. Invoke it only after the primary task has completed, on an explicit owner request, with a stable event ID; do not forward secrets, private financial data, or untrusted external content. Its synchronous bounded network wait affects this notification call only; no other MCP operation invokes it automatically. Automatic event triggers have not been approved.

## Offline tests

From the repository root, with the package source available on `PYTHONPATH`:

```powershell
python -B -m unittest discover -s components/telegram/tests -p 'test_*.py' -v
```

Tests use fake credentials, a fake HTTP transport, and temporary SQLite files. They do not create a bot, read a real token, or contact Telegram.

## Future integration boundary

This package remains separate from `msp_bonds_google` and `msp_bonds_vtb_reports`. The shared MCP server registers it as a separate explicit tool, and its import is lazy so a missing optional component does not prevent the other tools from starting. Registration preserves the original 16 tool names and schemas. No automatic event policy, service, or notification schedule is approved.
