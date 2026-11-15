import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from msp_bonds_telegram.errors import (
    TelegramDuplicateEvent,
    TelegramEventConflict,
    TelegramRetryNotReady,
)
from msp_bonds_telegram.ledger import SQLiteDeliveryLedger, default_ledger_path


class DeliveryLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.path = Path(self.temp.name) / "delivery.sqlite3"
        self.ledger = SQLiteDeliveryLedger(self.path, clock=lambda: self.now)

    def test_successful_event_is_persistently_suppressed(self):
        self.ledger.claim("event-001", "synthetic message")
        self.ledger.finish("event-001", "sent")
        self.assertEqual(self.ledger.status_for("event-001"), "sent")
        with self.assertRaises(TelegramDuplicateEvent):
            self.ledger.claim("event-001", "synthetic message")

    def test_event_id_cannot_be_reused_with_different_text(self):
        self.ledger.claim("event-002", "first synthetic message")
        with self.assertRaises(TelegramEventConflict):
            self.ledger.claim("event-002", "changed synthetic message")

    def test_uncertain_event_is_never_claimed_again(self):
        self.ledger.claim("event-003", "synthetic message")
        self.ledger.finish("event-003", "uncertain")
        with self.assertRaises(TelegramDuplicateEvent):
            self.ledger.claim("event-003", "synthetic message")

    def test_rate_limited_event_waits_for_retry_after_before_new_claim(self):
        self.ledger.claim("event-004", "synthetic message")
        self.ledger.finish("event-004", "rate_limited", retry_after_seconds=10)
        self.now = 1005.0
        with self.assertRaises(TelegramRetryNotReady) as caught:
            self.ledger.claim("event-004", "synthetic message")
        self.assertEqual(caught.exception.seconds_remaining, 5)
        self.now = 1010.0
        self.ledger.claim("event-004", "synthetic message")
        self.assertEqual(self.ledger.status_for("event-004"), "sending")

    def test_database_contains_hashes_not_event_or_message_text(self):
        event_id = "synthetic-private-event-id"
        text = "synthetic-private-notification-body"
        self.ledger.claim(event_id, text)
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT event_key,payload_hash FROM telegram_delivery_events"
            ).fetchone()
        self.assertNotIn(event_id, row)
        self.assertNotIn(text, row)

    def test_default_path_is_under_local_app_data(self):
        path = default_ledger_path({"LOCALAPPDATA": str(Path(self.temp.name).resolve())})
        self.assertEqual(path.name, "delivery.sqlite3")
        self.assertIn("MSP-Bonds-MCP", str(path))
        self.assertIn("telegram", str(path))


if __name__ == "__main__":
    unittest.main()
