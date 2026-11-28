"""Journal tests use only disposable temporary SQLite databases."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from msp_bonds_vtb_reports.classifier import BROKER_REPORT
from msp_bonds_vtb_reports.import_journal import ImportJournal, default_journal_path


class ImportJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vtb-journal-test-")
        self.addCleanup(self.temporary.cleanup)
        self.db = Path(self.temporary.name) / "runtime" / "vtb_imports.sqlite3"
        self.journal = ImportJournal(self.db)
        self.base = {
            "gmail_message_id": "synthetic-message-1",
            "attachment_id": "synthetic-attachment-1",
            "sha256": hashlib.sha256(b"synthetic report A").hexdigest(),
            "category": BROKER_REPORT,
            "account_ids": ("40702810000000000001",),
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "classification_status": "CLASSIFIED",
        }

    def register(self, **changes):
        data = dict(self.base)
        data.update(changes)
        return self.journal.register_attempt(**data)

    def test_default_path_is_project_runtime_path_without_side_effects(self):
        before = default_journal_path().exists()
        self.assertEqual(default_journal_path().name, "vtb_imports.sqlite3")
        self.assertEqual(default_journal_path().exists(), before)

    def test_new_document_and_metadata_only_schema(self):
        result = self.register()
        self.assertEqual(result.status, "NEW_DOCUMENT")
        self.assertEqual(self.journal.count(), 1)
        columns = set(self.journal.columns())
        self.assertIn("gmail_message_id", columns)
        self.assertIn("attachment_id", columns)
        self.assertIn("sha256", columns)
        self.assertIn("drive_file_id", columns)
        self.assertFalse(columns & {"message_body", "email_body", "attachment_bytes", "document_bytes", "transactions"})

    def test_same_hash_in_another_attachment_is_exact_duplicate(self):
        self.register()
        duplicate = self.register(attachment_id="synthetic-attachment-2")
        self.assertEqual(duplicate.status, "EXACT_DUPLICATE")
        self.assertEqual(self.journal.count(), 2)

    def test_same_document_in_another_message_is_repeat_email(self):
        self.register()
        repeat = self.register(gmail_message_id="synthetic-message-2", attachment_id="synthetic-attachment-other-message")
        self.assertEqual(repeat.status, "REPEAT_MESSAGE")

    def test_same_exact_type_account_period_with_different_bytes_is_conflict(self):
        self.register()
        changed = self.register(
            gmail_message_id="synthetic-message-2",
            attachment_id="synthetic-attachment-2",
            sha256=hashlib.sha256(b"synthetic report B").hexdigest(),
        )
        self.assertEqual(changed.status, "CONFLICT_VERSION")
        self.assertIn("different bytes", changed.reason)

    def test_overlapping_but_not_equal_periods_are_not_duplicates(self):
        self.register()
        overlapping = self.register(
            gmail_message_id="synthetic-message-2",
            attachment_id="synthetic-attachment-2",
            sha256=hashlib.sha256(b"overlapping report").hexdigest(),
            period_start="2026-01-15",
            period_end="2026-02-15",
        )
        self.assertEqual(overlapping.status, "NEW_DOCUMENT")

    def test_missing_account_is_held_for_review(self):
        result = self.register(account_ids=())
        self.assertEqual(result.status, "NEEDS_REVIEW")

    def test_retry_after_error_updates_same_row_and_increments_attempts(self):
        failed = self.register(error_reason="synthetic extraction error")
        retried = self.register(error_reason=None)
        self.assertEqual(failed.status, "ERROR")
        self.assertEqual(retried.status, "NEW_DOCUMENT")
        self.assertEqual(retried.attempts, 2)
        self.assertEqual(self.journal.count(), 1)

    def test_unique_hash_without_period_requires_review(self):
        result = self.register(
            sha256=hashlib.sha256(b"synthetic no-period report").hexdigest(),
            period_start=None,
            period_end=None,
        )
        self.assertEqual(result.status, "NEEDS_REVIEW")

    def test_exact_hash_match_is_detected_even_when_new_metadata_is_incomplete(self):
        self.register()
        duplicate = self.register(
            attachment_id="synthetic-attachment-duplicate",
            account_ids=(),
            period_start=None,
            period_end=None,
            classification_status="NEEDS_REVIEW",
        )
        self.assertEqual(duplicate.status, "EXACT_DUPLICATE")

    def test_repeated_processing_is_idempotent_and_drive_id_stays_empty_until_confirmed(self):
        first = self.register()
        second = self.register()
        self.assertEqual(first.status, second.status)
        self.assertEqual(second.attempts, 2)
        self.assertEqual(self.journal.count(), 1)
        self.assertIsNone(self.journal.records()[0]["drive_file_id"])

    def test_changed_bytes_for_same_gmail_attachment_are_conflict_without_overwrite(self):
        original = self.register()
        changed_hash = hashlib.sha256(b"synthetic changed bytes").hexdigest()
        conflict = self.register(sha256=changed_hash)
        self.assertEqual(conflict.status, "CONFLICT_VERSION")
        rows = self.journal.records()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["sha256"] for row in rows}, {self.base["sha256"], changed_hash})
        self.assertEqual(original.status, "NEW_DOCUMENT")

    def test_mark_imported_requires_known_drive_id_and_is_idempotent(self):
        new = self.register()
        imported = self.journal.mark_imported(
            gmail_message_id=self.base["gmail_message_id"],
            attachment_id=self.base["attachment_id"],
            sha256=self.base["sha256"],
            drive_file_id="synthetic-drive-file-id",
        )
        self.assertEqual(new.status, "NEW_DOCUMENT")
        self.assertEqual(imported.status, "IMPORTED")
        self.assertEqual(imported.drive_file_id, "synthetic-drive-file-id")
        repeated = self.journal.mark_imported(
            gmail_message_id=self.base["gmail_message_id"],
            attachment_id=self.base["attachment_id"],
            sha256=self.base["sha256"],
            drive_file_id="synthetic-drive-file-id",
        )
        self.assertEqual(repeated.status, "IMPORTED")
        self.assertEqual(self.journal.count(), 1)
        self.assertEqual(self.journal.records()[0]["drive_file_id"], "synthetic-drive-file-id")


if __name__ == "__main__":
    unittest.main()
