"""Synthetic-only tests, no credentials, no Google network, no broker data."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from msp_bonds_google.utils import (InputError, attachment_parts, decode_base64url,
                                        drive_q_literal, safe_filename, sha256_bytes)
from msp_bonds_google import approvals as ap
from msp_bonds_google import workspace as gw
from msp_bonds_google import writes


class GoogleHelpersTests(unittest.TestCase):
    def test_clean_filename_and_escaping(self):
        self.assertEqual(safe_filename('../../my.pdf'), 'my.pdf')
        self.assertEqual(safe_filename('C:\\temp\\x.xlsx'), 'x.xlsx')
        self.assertEqual(drive_q_literal("O'Reilly"), "O\\'Reilly")
        self.assertEqual(decode_base64url('SGVsbG8'), b'Hello')
        self.assertEqual(len(sha256_bytes(b'fake')), 64)

    def test_message_parts_with_wrong_attachment_mime(self):
        p = {"parts": [{"filename": "sample.xlsx", "partId": "1", "mimeType": "application/octet-stream",
                         "body": {"attachmentId": "A", "size": 4}}]}
        v = attachment_parts(p)
        self.assertEqual(v[0]["filename"], "sample.xlsx")
        self.assertEqual(v[0]["attachment_id"], "A")

    def test_drive_list_uses_known_parent_and_returns_page(self):
        svc = MagicMock()
        svc.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "f1", "name": "sample"}], "nextPageToken": "page2"}
        with patch.object(gw, "_drive", return_value=svc):
            result = gw.drive_list(parent_id="folder",name_contains="O'Reilly",page_size=20)
        self.assertEqual(result["files"][0]["id"], "f1")
        q = svc.files.return_value.list.call_args.kwargs["q"]
        self.assertIn("'folder' in parents", q)
        self.assertIn("O\\'Reilly", q)
        self.assertEqual(result["next_page_token"], "page2")

    def test_gmail_attachment_size_is_checked_before_attachment_download(self):
        mock = MagicMock()
        mock.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "payload": {"parts": [{"partId": "1", "filename": "large.pdf",
                                   "body": {"attachmentId": "A", "size": gw.MAX_BYTES + 1}}]}}
        with patch.object(gw, "_gmail", return_value=mock):
            with self.assertRaises(InputError):
                gw.gmail_attachment_bytes("mid", "1")
        mock.users.return_value.messages.return_value.attachments.return_value.get.assert_not_called()

    def test_gmail_attachment_bytes_stay_in_memory_for_octet_stream(self):
        mock = MagicMock()
        mock.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "payload": {"parts": [{"partId": "1", "filename": "report.bin",
                                   "mimeType": "application/octet-stream",
                                   "body": {"attachmentId": "A", "size": 4}}]}}
        mock.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.return_value = {
            "data": "UEsDBA=="}
        with patch.object(gw, "_gmail", return_value=mock):
            result = gw.gmail_attachment_bytes("mid", "1")
        self.assertEqual(result["data"], b"PK\x03\x04")
        self.assertEqual(result["mime_type"], "application/octet-stream")
        self.assertEqual(result["size"], 4)

    def test_gmail_message_manifest_does_not_request_body_bytes(self):
        mock = MagicMock()
        mock.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "id": "mid", "payload": {"headers": [], "parts": []}}
        with patch.object(gw, "_gmail", return_value=mock):
            gw.gmail_message("mid", include_body=False)
        kwargs = mock.users.return_value.messages.return_value.get.call_args.kwargs
        self.assertNotIn("data", kwargs["fields"])
        self.assertIn("size", kwargs["fields"])
        self.assertIn("attachmentId", kwargs["fields"])

    def test_inline_attachment_without_separate_id_is_rejected_safely(self):
        mock = MagicMock()
        mock.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "payload": {"parts": [{"partId": "1", "filename": "report.pdf",
                                   "body": {"size": 4}}]}}
        with patch.object(gw, "_gmail", return_value=mock):
            with self.assertRaises(InputError):
                gw.gmail_attachment_bytes("mid", "1")
        mock.users.return_value.messages.return_value.attachments.return_value.get.assert_not_called()

    def test_gmail_missing_declared_size_never_downloads_attachment(self):
        mock = MagicMock()
        mock.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "payload": {"parts": [{"partId": "1", "filename": "report.pdf",
                                   "body": {"attachmentId": "A"}}]}}
        with patch.object(gw, "_gmail", return_value=mock):
            with self.assertRaises(InputError):
                gw.gmail_attachment_bytes("mid", "1")
        mock.users.return_value.messages.return_value.attachments.return_value.get.assert_not_called()

    def test_gmail_staging_only_no_cloud_write(self):
        mock = MagicMock()
        mock.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "payload": {"parts": [{"partId": "1", "filename": "../synthetic.xlsx",
                                   "body": {"attachmentId": "A"}}]}}
        mock.users.return_value.messages.return_value.attachments.return_value.get.return_value.execute.return_value = {
            "data": "UEsDBA=="}
        with tempfile.TemporaryDirectory() as d:
            with patch.object(gw, "_gmail", return_value=mock), patch.object(gw, "staging_dir", return_value=Path(d)):
                result = gw.gmail_stage_attachment("mid", "1")
                self.assertEqual(Path(d, result["staged_name"]).read_bytes(), b'PK\x03\x04')
                self.assertFalse(result["uploaded_to_drive"])


class GoogleApprovalTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.patch = patch.object(ap, "_db_path", return_value=Path(self.t.name) / "approvals.sqlite3")
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_proposal_needs_external_human_approval(self):
        plan = ap.propose("drive_create_text",json.dumps({"parent_id": "folder", "name": "test.txt", "text": "abc"}))
        self.assertEqual(ap.get_plan(plan["plan_id"])["status"], "PENDING")
        with self.assertRaises(InputError):
            ap.consume_approved(plan["plan_id"])
        with patch.dict(os.environ,{"MSP_GOOGLE_WRITES_ENABLED": "0"}):
            with self.assertRaises(InputError):
                writes.execute(plan["plan_id"])
        self.assertEqual(ap.get_plan(plan["plan_id"])["status"], "PENDING")
        with patch("builtins.input", return_value="APPROVE"):
            ap.approve_interactively(plan["plan_id"])
        self.assertEqual(ap.consume_approved(plan["plan_id"])["action"], "drive_create_text")
        with self.assertRaises(InputError):
            ap.consume_approved(plan["plan_id"])

    def test_forbidden_write_rejected(self):
        with self.assertRaises(InputError):
            ap.propose("gmail_send", '{"id":"abc"}')
        with self.assertRaises(InputError):
            ap.propose("drive_delete_permanent", '{"file_id":"abc"}')


if __name__ == '__main__':
    unittest.main()
