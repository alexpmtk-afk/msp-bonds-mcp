"""Synchronous, fixed-destination, best-effort Telegram notification delivery."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

from .config import TelegramSettings
from .credentials import WindowsCredentialProvider
from .errors import (
    TelegramApiRejected,
    TelegramComponentError,
    TelegramDuplicateEvent,
    TelegramEventConflict,
    TelegramOutcomeUnknown,
    TelegramRateLimitDeferred,
    TelegramRetryNotReady,
)
from .ledger import SQLiteDeliveryLedger
from .transport import HttpResponse, TelegramTransport, UrllibTelegramTransport

MAX_MESSAGE_CHARACTERS = 4096
HTTP_TIMEOUT_SECONDS = 8.0
MAX_ATTEMPTS = 2
MAX_INLINE_RETRY_AFTER_SECONDS = 5


@dataclass(frozen=True, slots=True)
class SendReceipt:
    message_id: int


@dataclass(frozen=True, slots=True)
class NotificationOutcome:
    status: str
    error_code: str | None = None


class TelegramSender:
    """Send plain text to one configured group/topic; callers cannot choose a recipient."""

    def __init__(
        self,
        settings: TelegramSettings,
        credential_provider: WindowsCredentialProvider,
        *,
        transport: TelegramTransport | None = None,
        ledger: SQLiteDeliveryLedger | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._credentials = credential_provider
        self._transport = transport or UrllibTelegramTransport()
        self._ledger = ledger or SQLiteDeliveryLedger(clock=clock)
        self._sleep = sleeper
        self._clock = clock

    def send_event(self, event_id: str, text: str) -> SendReceipt:
        """Send one event using only the fixed settings destination."""

        _validate_event(event_id, text)
        token = self._credentials.get_token()
        try:
            self._ledger.claim(event_id, text)
        except TelegramRetryNotReady as error:
            raise TelegramRateLimitDeferred(error.seconds_remaining) from None

        try:
            receipt = self._send_with_limited_retry(token, text)
        except TelegramRateLimitDeferred as error:
            if error.retry_after is None:
                self._ledger.finish(event_id, "rejected")
            else:
                self._ledger.finish(event_id, "rate_limited", retry_after_seconds=error.retry_after)
            raise
        except TelegramApiRejected:
            self._ledger.finish(event_id, "rejected")
            raise
        except TelegramOutcomeUnknown:
            self._ledger.finish(event_id, "uncertain")
            raise
        except Exception:
            # Any unclassified failure after the claim is conservatively uncertain.
            # A later invocation will be blocked by the persisted event ID.
            self._ledger.finish(event_id, "uncertain")
            raise

        self._ledger.finish(event_id, "sent")
        return receipt

    def _send_with_limited_retry(self, token: str, text: str) -> SendReceipt:
        for attempt in range(MAX_ATTEMPTS):
            response = self._transport.send_message(
                token,
                chat_id=self._settings.chat_id,
                message_thread_id=self._settings.message_thread_id,
                text=text,
                timeout_seconds=HTTP_TIMEOUT_SECONDS,
            )
            document = _decode_document(response)
            effective_status = _effective_status(response.status_code, document)

            if effective_status == 429:
                retry_after = _retry_after(document)
                if retry_after is not None and retry_after <= MAX_INLINE_RETRY_AFTER_SECONDS:
                    if attempt + 1 < MAX_ATTEMPTS:
                        self._sleep(retry_after)
                        continue
                if retry_after is not None:
                    raise TelegramRateLimitDeferred(retry_after)
                raise TelegramApiRejected(429)

            if effective_status >= 500:
                raise TelegramOutcomeUnknown()
            if effective_status < 200 or effective_status >= 300:
                raise TelegramApiRejected(effective_status)

            if not isinstance(document, dict):
                raise TelegramOutcomeUnknown()
            if document.get("ok") is not True:
                code = document.get("error_code")
                if isinstance(code, int) and not isinstance(code, bool) and 400 <= code < 500:
                    raise TelegramApiRejected(code)
                raise TelegramOutcomeUnknown()

            result = document.get("result")
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
                raise TelegramOutcomeUnknown()
            return SendReceipt(message_id)

        # The loop can only exhaust after Telegram returned a 429 without an
        # acceptable retry window; defensive fallback remains non-retryable.
        raise TelegramRateLimitDeferred(None)


def notify_best_effort(
    sender: TelegramSender,
    event_id: str,
    text: str,
) -> NotificationOutcome:
    """Return a small safe result so a caller's primary task can continue."""

    try:
        sender.send_event(event_id, text)
        return NotificationOutcome("sent")
    except TelegramDuplicateEvent:
        return NotificationOutcome("duplicate", "duplicate_event")
    except TelegramEventConflict:
        return NotificationOutcome("blocked", "event_id_conflict")
    except TelegramRateLimitDeferred:
        return NotificationOutcome("deferred", "rate_limited")
    except TelegramOutcomeUnknown:
        return NotificationOutcome("uncertain", "delivery_outcome_unknown")
    except TelegramApiRejected as error:
        return NotificationOutcome("failed", f"telegram_http_{error.status_code}")
    except TelegramComponentError:
        return NotificationOutcome("failed", "telegram_component_error")
    except Exception:
        # Deliberately do not log exception text: it could contain the tokenized URL.
        return NotificationOutcome("failed", "internal_error")


def _validate_event(event_id: str, text: str) -> None:
    if not isinstance(event_id, str) or not event_id or len(event_id) > 256 or event_id != event_id.strip():
        raise ValueError("event_id must be a non-empty stable identifier of at most 256 characters.")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Notification text must be a non-empty string.")
    if len(text) > MAX_MESSAGE_CHARACTERS:
        raise ValueError("Notification text exceeds the Telegram Bot API message limit.")


def _decode_document(response: HttpResponse) -> dict | None:
    if len(response.body) > 64 * 1024:
        raise TelegramOutcomeUnknown()
    try:
        document = json.loads(response.body.decode("utf-8"))
    except Exception:
        # A malformed success response cannot prove whether the message was sent.
        if 200 <= response.status_code < 300:
            raise TelegramOutcomeUnknown() from None
        return None
    return document if isinstance(document, dict) else None


def _effective_status(http_status: int, document: dict | None) -> int:
    if http_status == 429:
        return 429
    if document is not None:
        code = document.get("error_code")
        if isinstance(code, int) and not isinstance(code, bool) and code == 429:
            return 429
        if http_status < 400 and isinstance(code, int) and not isinstance(code, bool):
            return code
    return http_status


def _retry_after(document: dict | None) -> int | None:
    if document is None:
        return None
    parameters = document.get("parameters")
    value = parameters.get("retry_after") if isinstance(parameters, dict) else None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return value
