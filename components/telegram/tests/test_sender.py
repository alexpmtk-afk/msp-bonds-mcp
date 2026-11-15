import inspect
import json
import tempfile
import unittest
from pathlib import Path

from msp_bonds_telegram.config import TelegramSettings
from msp_bonds_telegram.errors import (
    TelegramApiRejected,
    TelegramDuplicateEvent,
    TelegramEventConflict,
    TelegramOutcomeUnknown,
    TelegramRateLimitDeferred,
)
from msp_bonds_telegram.ledger import SQLiteDeliveryLedger
from msp_bonds_telegram.sender import (
    HTTP_TIMEOUT_SECONDS,
    MAX_MESSAGE_CHARACTERS,
    TelegramSender,
    notify_best_effort,
)
from msp_bonds_telegram.transport import HttpResponse


def response(status, document):
    return HttpResponse(status, json.dumps(document).encode("utf-8"))


class FakeCredentials:
    def __init__(self, token="synthetic-token"):
        self.token = token
        self.read_count = 0

    def get_token(self):
        self.read_count += 1
        return self.token


class FakeTransport:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def send_message(self, token, **kwargs):
        self.calls.append((token, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class TelegramSenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.waits = []
        self.settings = TelegramSettings(-1001234567890, 42, True)
        self.credentials = FakeCredentials()

    def make_sender(self, transport):
        ledger = SQLiteDeliveryLedger(
            Path(self.temp.name) / f"ledger-{len(list(Path(self.temp.name).glob('*.sqlite3')))}.sqlite3",
            clock=lambda: self.now,
        )
        return TelegramSender(
            self.settings,
            self.credentials,
            transport=transport,
            ledger=ledger,
            sleeper=self._sleep,
            clock=lambda: self.now,
        ), ledger

    def _sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def test_success_uses_only_fixed_group_and_topic(self):
        transport = FakeTransport(response(200, {"ok": True, "result": {"message_id": 7}}))
        sender, ledger = self.make_sender(transport)
        receipt = sender.send_event("event-success", "Synthetic notification")

        self.assertEqual(receipt.message_id, 7)
        token, payload = transport.calls[0]
        self.assertEqual(token, "synthetic-token")
        self.assertEqual(
            payload,
            {
                "chat_id": -1001234567890,
                "message_thread_id": 42,
                "text": "Synthetic notification",
                "timeout_seconds": HTTP_TIMEOUT_SECONDS,
            },
        )
        self.assertEqual(ledger.status_for("event-success"), "sent")

    def test_send_api_has_no_recipient_arguments(self):
        parameters = inspect.signature(TelegramSender.send_event).parameters
        self.assertEqual(tuple(parameters), ("self", "event_id", "text"))
        transport = FakeTransport(response(200, {"ok": True, "result": {"message_id": 8}}))
        sender, _ = self.make_sender(transport)
        with self.assertRaises(TypeError):
            sender.send_event("event-target", "text", chat_id=-1009999999999)
        self.assertEqual(transport.calls, [])

    def test_access_denial_is_not_retried_or_exposed(self):
        body = {"ok": False, "error_code": 403, "description": "Forbidden synthetic-token"}
        transport = FakeTransport(response(403, body))
        sender, ledger = self.make_sender(transport)
        with self.assertRaises(TelegramApiRejected) as caught:
            sender.send_event("event-403", "Synthetic notification")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertNotIn("synthetic-token", str(caught.exception))
        self.assertNotIn("Forbidden", str(caught.exception))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(ledger.status_for("event-403"), "rejected")

    def test_invalid_topic_is_a_definitive_400_rejection(self):
        transport = FakeTransport(
            response(400, {"ok": False, "error_code": 400, "description": "message thread not found"})
        )
        sender, ledger = self.make_sender(transport)
        with self.assertRaises(TelegramApiRejected) as caught:
            sender.send_event("event-topic", "Synthetic notification")
        self.assertEqual(caught.exception.status_code, 400)
        self.assertNotIn("thread not found", str(caught.exception))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(ledger.status_for("event-topic"), "rejected")

    def test_429_retries_once_after_short_retry_after(self):
        transport = FakeTransport(
            response(429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 2}}),
            response(200, {"ok": True, "result": {"message_id": 9}}),
        )
        sender, ledger = self.make_sender(transport)
        receipt = sender.send_event("event-429-short", "Synthetic notification")
        self.assertEqual(receipt.message_id, 9)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(self.waits, [2])
        self.assertEqual(ledger.status_for("event-429-short"), "sent")

    def test_long_429_retry_after_is_deferred_without_sleeping(self):
        transport = FakeTransport(
            response(429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 30}}),
            response(200, {"ok": True, "result": {"message_id": 10}}),
        )
        sender, ledger = self.make_sender(transport)
        with self.assertRaises(TelegramRateLimitDeferred) as caught:
            sender.send_event("event-429-long", "Synthetic notification")
        self.assertEqual(caught.exception.retry_after, 30)
        self.assertEqual(self.waits, [])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(ledger.status_for("event-429-long"), "rate_limited")

        with self.assertRaises(TelegramRateLimitDeferred):
            sender.send_event("event-429-long", "Synthetic notification")
        self.assertEqual(len(transport.calls), 1)

        self.now += 30
        receipt = sender.send_event("event-429-long", "Synthetic notification")
        self.assertEqual(receipt.message_id, 10)
        self.assertEqual(len(transport.calls), 2)

    def test_missing_retry_after_is_not_guessed_or_retried(self):
        transport = FakeTransport(response(429, {"ok": False, "error_code": 429}))
        sender, ledger = self.make_sender(transport)
        with self.assertRaises(TelegramApiRejected) as caught:
            sender.send_event("event-429-missing", "Synthetic notification")
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(ledger.status_for("event-429-missing"), "rejected")

    def test_network_error_marks_uncertain_and_blocks_repeat(self):
        transport = FakeTransport(TelegramOutcomeUnknown(), response(200, {"ok": True, "result": {"message_id": 11}}))
        sender, ledger = self.make_sender(transport)
        with self.assertRaises(TelegramOutcomeUnknown):
            sender.send_event("event-network", "Synthetic notification")
        self.assertEqual(ledger.status_for("event-network"), "uncertain")
        with self.assertRaises(TelegramDuplicateEvent):
            sender.send_event("event-network", "Synthetic notification")
        self.assertEqual(len(transport.calls), 1)

    def test_server_error_is_uncertain_and_not_retried(self):
        transport = FakeTransport(response(500, {"ok": False, "error_code": 500}))
        sender, ledger = self.make_sender(transport)
        with self.assertRaises(TelegramOutcomeUnknown):
            sender.send_event("event-500", "Synthetic notification")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(ledger.status_for("event-500"), "uncertain")

    def test_duplicate_event_and_conflicting_payload_are_suppressed(self):
        transport = FakeTransport(response(200, {"ok": True, "result": {"message_id": 12}}))
        sender, _ = self.make_sender(transport)
        sender.send_event("event-duplicate", "First text")
        with self.assertRaises(TelegramDuplicateEvent):
            sender.send_event("event-duplicate", "First text")
        with self.assertRaises(TelegramEventConflict):
            sender.send_event("event-duplicate", "Different text")
        self.assertEqual(len(transport.calls), 1)

    def test_missing_credentials_do_not_start_network_request(self):
        class MissingCredentials:
            def get_token(self):
                from msp_bonds_telegram.errors import TelegramCredentialUnavailable

                raise TelegramCredentialUnavailable("No synthetic token")

        transport = FakeTransport(response(200, {"ok": True, "result": {"message_id": 13}}))
        sender = TelegramSender(
            self.settings,
            MissingCredentials(),
            transport=transport,
            ledger=SQLiteDeliveryLedger(Path(self.temp.name) / "no-token.sqlite3", clock=lambda: self.now),
        )
        outcome = notify_best_effort(sender, "event-no-token", "Synthetic notification")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(transport.calls, [])

    def test_best_effort_wrapper_never_propagates_delivery_failure(self):
        transport = FakeTransport(response(403, {"ok": False, "error_code": 403}))
        sender, _ = self.make_sender(transport)
        outcome = notify_best_effort(sender, "event-best-effort", "Synthetic notification")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error_code, "telegram_http_403")

    def test_text_limit_is_checked_before_credentials_or_transport(self):
        transport = FakeTransport(response(200, {"ok": True, "result": {"message_id": 14}}))
        sender, _ = self.make_sender(transport)
        with self.assertRaises(ValueError):
            sender.send_event("event-long", "x" * (MAX_MESSAGE_CHARACTERS + 1))
        self.assertEqual(self.credentials.read_count, 0)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
