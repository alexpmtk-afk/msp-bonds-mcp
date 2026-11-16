"""Safe callable boundary for registering Telegram as an MCP tool.

This module has no dependency on the MCP SDK. The server can register the
exported function without moving Telegram transport or policy into core.
"""

from __future__ import annotations

from collections.abc import Callable

from .config import TelegramSettings
from .credentials import WindowsCredentialProvider
from .errors import TelegramComponentError, TelegramConfigurationError
from .sender import NotificationOutcome, TelegramSender, notify_best_effort


def create_telegram_notification_handler(
    *,
    settings_factory: Callable[[], TelegramSettings] = TelegramSettings.from_environment,
    credential_provider_factory: Callable[[], WindowsCredentialProvider] = WindowsCredentialProvider,
    sender_factory: Callable[[TelegramSettings, WindowsCredentialProvider], TelegramSender] = TelegramSender,
    notifier: Callable[[TelegramSender, str, str], NotificationOutcome] = notify_best_effort,
) -> Callable[[str, str], dict]:
    """Build an MCP-compatible callable with injectable offline-test dependencies."""

    def send_telegram_notification(event_id: str, text: str) -> dict:
        """Send one owner-requested message to the configured fixed Telegram topic.

        This is a separate, explicit operation. Do not call it automatically for
        ordinary MCP tasks, and do not forward secrets, private financial data,
        or untrusted external content.
        """

        try:
            settings = settings_factory()
            credentials = credential_provider_factory()
            sender = sender_factory(settings, credentials)
            outcome = notifier(sender, event_id, text)
            return {"status": outcome.status, "error_code": outcome.error_code}
        except TelegramConfigurationError:
            return {"status": "failed", "error_code": "configuration_error"}
        except TelegramComponentError:
            return {"status": "failed", "error_code": "telegram_component_error"}
        except Exception:
            # Do not return exception text: a transport error could contain a
            # tokenized URL or other sensitive provider details.
            return {"status": "failed", "error_code": "internal_error"}

    return send_telegram_notification


send_telegram_notification = create_telegram_notification_handler()
