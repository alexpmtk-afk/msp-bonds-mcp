import inspect
import unittest

from msp_bonds_telegram.errors import TelegramConfigurationError
from msp_bonds_telegram.mcp_tool import (
    create_telegram_notification_handler,
    send_telegram_notification,
)
from msp_bonds_telegram.sender import NotificationOutcome


class TelegramMcpToolTests(unittest.TestCase):
    def test_public_callable_has_only_event_and_text_arguments(self):
        self.assertEqual(
            tuple(inspect.signature(send_telegram_notification).parameters),
            ("event_id", "text"),
        )

    def test_success_returns_a_small_safe_result(self):
        sender_marker = object()
        seen = []
        handler = create_telegram_notification_handler(
            settings_factory=lambda: "fixed-settings",
            credential_provider_factory=lambda: "credential-provider",
            sender_factory=lambda settings, credentials: sender_marker,
            notifier=lambda sender, event_id, text: (
                seen.append((sender, event_id, text)) or NotificationOutcome("sent")
            ),
        )

        result = handler("stable-event-1", "Owner-approved notice")

        self.assertEqual(result, {"status": "sent", "error_code": None})
        self.assertEqual(seen, [(sender_marker, "stable-event-1", "Owner-approved notice")])

    def test_missing_configuration_is_reported_without_running_sender(self):
        sender_calls = []
        handler = create_telegram_notification_handler(
            settings_factory=lambda: (_ for _ in ()).throw(TelegramConfigurationError("private config")),
            sender_factory=lambda *args: sender_calls.append(args),
        )

        result = handler("stable-event-2", "Owner-approved notice")

        self.assertEqual(result, {"status": "failed", "error_code": "configuration_error"})
        self.assertEqual(sender_calls, [])

    def test_unexpected_failure_is_sanitized_and_does_not_escape(self):
        handler = create_telegram_notification_handler(
            settings_factory=lambda: "fixed-settings",
            credential_provider_factory=lambda: "credential-provider",
            sender_factory=lambda *args: object(),
            notifier=lambda *args: (_ for _ in ()).throw(RuntimeError("https://api.telegram.org/botSECRET")),
        )

        result = handler("stable-event-3", "Owner-approved notice")

        self.assertEqual(result, {"status": "failed", "error_code": "internal_error"})
        self.assertNotIn("SECRET", repr(result))


if __name__ == "__main__":
    unittest.main()
