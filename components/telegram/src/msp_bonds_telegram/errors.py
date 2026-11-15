"""Sanitized errors for Telegram notification delivery."""


class TelegramComponentError(Exception):
    """Base class whose messages never contain request bodies or credentials."""


class TelegramConfigurationError(TelegramComponentError):
    """A fixed target or required setting is missing or invalid."""


class TelegramCredentialError(TelegramComponentError):
    """The token could not be safely retrieved from Windows Credential Locker."""


class TelegramCredentialBackendError(TelegramCredentialError):
    """The active keyring is not the expected Windows Credential Locker backend."""


class TelegramCredentialUnavailable(TelegramCredentialError):
    """No usable token is stored in the protected local credential store."""


class TelegramDeliveryError(TelegramComponentError):
    """A delivery attempt was not confirmed as successful."""


class TelegramApiRejected(TelegramDeliveryError):
    """Telegram returned a definitive rejection; response text is not retained."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Telegram rejected the notification (HTTP {status_code}).")


class TelegramRateLimitDeferred(TelegramDeliveryError):
    """Telegram returned 429 and the bounded retry policy deferred delivery."""

    def __init__(self, retry_after: int | None):
        self.retry_after = retry_after
        super().__init__("Telegram rate limit deferred this notification.")


class TelegramOutcomeUnknown(TelegramDeliveryError):
    """The request may have reached Telegram; retrying could send a duplicate."""

    def __init__(self):
        super().__init__("Telegram delivery outcome is unknown; automatic retry is blocked.")


class TelegramDuplicateEvent(TelegramDeliveryError):
    """The event ID has already been claimed or completed."""

    def __init__(self):
        super().__init__("This notification event was already processed; no message was sent.")


class TelegramEventConflict(TelegramDeliveryError):
    """An existing event ID was reused with different text."""

    def __init__(self):
        super().__init__("This notification event ID is already bound to different content.")


class TelegramRetryNotReady(TelegramDeliveryError):
    """A prior definitive 429 response has not reached its retry-after time."""

    def __init__(self, seconds_remaining: int):
        self.seconds_remaining = max(1, seconds_remaining)
        super().__init__("Telegram has not reached the recorded retry-after time.")


class TelegramLedgerError(TelegramComponentError):
    """The local deduplication ledger could not be read or written safely."""
