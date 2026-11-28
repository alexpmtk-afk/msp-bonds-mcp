"""Synthetic, offline-only tests for Gmail VTB report discovery."""
from __future__ import annotations

import hashlib
import io
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from msp_bonds_vtb_reports.archive import REPORT_CATEGORIES
from msp_bonds_vtb_reports.gmail_discovery import (
    MAX_ATTACHMENT_BYTES,
    normalize_sender_allowlist,
    scan_gmail_reports,
)
from msp_bonds_vtb_reports.text_extraction import ExtractionResult
from msp_bonds_mcp.core import server as mcp_server


BROKER = REPORT_CATEGORIES[0]
BROKER_ID = "Label_Broker"


def synthetic_xlsx(text: str) -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        escaped = (text.replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;"))
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            "<worksheet><sheetData><row><c r='A1' t='inlineStr'><is><t>"
            + escaped
            + "</t></is></c></row></sheetData></worksheet>",
        )
    return buffer.getvalue()


def report_text(period_start: str = "01.03.2026", period_end: str = "31.03.2026") -> str:
    return (
        "Брокерский отчёт. Сделки. Денежные операции и остатки. "
        "Брокерский счёт № ACC12345. "
        f"Отчётный период: {period_start} — {period_end}."
    )


def archive_row(
    *,
    digest: str = "0" * 64,
    period_start: str = "2026-01-01",
    period_end: str = "2026-01-31",
    account: str = "ACC12345",
    category: str = BROKER,
) -> dict:
    return {
        "archive_ref": "V0001",
        "sha256": digest,
        "drive_verification": "MATCH",
        "drive_file_id": "synthetic-drive-file",
        "drive_parent_folder_id": "synthetic-folder",
        "detected_category": category,
        "classification_status": "CLASSIFIED",
        "classification_outcome": "PASS",
        "account_ids": [account],
        "period_start": period_start,
        "period_end": period_end,
    }


def attachment(data: bytes, *, filename="report.bin", mime="application/octet-stream",
               part_id="1", attachment_id="att-1", size=None) -> dict:
    return {
        "filename": filename,
        "mime_type": mime,
        "part_id": part_id,
        "attachment_id": attachment_id,
        "size": len(data) if size is None else size,
    }


class GmailDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.corporate_id = "Label_Corporate"
        self.allowlist = {"project-address-04@example.invalid"}
        self.message = {
            "labels": [BROKER_ID, self.corporate_id],
            "headers": {"from": "VTB Reports <project-address-04@example.invalid>"},
            "attachments": [],
        }

    def scan(self, data: bytes, *, archive_records=None, pages=None, get_message=None,
             allowed_senders=None, attachment_meta=None, get_attachment=None,
             max_pages=10):
        pages = pages or {"": {"messages": [{"id": "msg-1"}], "next_page_token": ""}}
        default_message = dict(self.message)
        if not default_message.get("attachments"):
            default_message["attachments"] = [
                attachment_meta or attachment(data)
            ]
        message_data = get_message or (lambda _message_id: default_message)
        fetch = get_attachment or (lambda _mid, _part, _limit: data)

        def search_page(*, label_id, page_size, page_token):
            self.assertEqual(label_id, BROKER_ID)
            self.assertGreaterEqual(page_size, 1)
            return pages[page_token]

        return scan_gmail_reports(
            search_page=search_page,
            get_message=message_data,
            get_attachment=fetch,
            archive_records=archive_records if archive_records is not None else [archive_row()],
            archive_complete=True,
            allowed_senders=self.allowlist if allowed_senders is None else allowed_senders,
            broker_label_id=BROKER_ID,
            corporate_action_label_id=self.corporate_id,
            page_size=10,
            max_pages=max_pages,
        )

    def test_unconfigured_oauth_stops_before_any_gmail_api_call(self):
        with patch.object(mcp_server, "token_state", return_value={"configured": False}):
            with patch.object(mcp_server.gw, "gmail_list_labels") as list_labels:
                result = mcp_server._scan_vtb_gmail_reports(10, "", 1)
        self.assertEqual(result["status"], "AUTH_REQUIRED")
        self.assertEqual(result["api_requests_made"], 0)
        list_labels.assert_not_called()

    def test_allowlist_requires_exact_addresses(self):
        self.assertEqual(
            normalize_sender_allowlist("Sender <project-address-04@example.invalid>; bad domain"),
            frozenset({"project-address-04@example.invalid"}),
        )

    def test_octet_stream_xlsx_is_new_and_paginated_duplicate_message_is_skipped(self):
        data = synthetic_xlsx(report_text())
        pages = {
            "": {"messages": [{"id": "msg-1"}], "next_page_token": "next"},
            "next": {"messages": [{"id": "msg-1"}], "next_page_token": ""},
        }
        message = dict(self.message)
        message["attachments"] = [attachment(data)]
        search_calls = []

        def search_page(*, label_id, page_size, page_token):
            search_calls.append((label_id, page_size, page_token))
            return pages[page_token]

        result = scan_gmail_reports(
            search_page=search_page,
            get_message=lambda _message_id: message,
            get_attachment=lambda _mid, _part, _limit: {"data": data},
            archive_records=[archive_row()],
            archive_complete=True,
            allowed_senders=self.allowlist,
            broker_label_id=BROKER_ID,
            corporate_action_label_id=self.corporate_id,
            page_size=10,
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["duplicate_messages_skipped"], 1)
        self.assertEqual(search_calls, [(BROKER_ID, 10, ""), (BROKER_ID, 10, "next")])
        self.assertEqual(result["result_counts"]["NEW"], 1)
        item = result["results"][0]
        self.assertEqual(item["category"], BROKER)
        self.assertEqual(item["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(item["mime_type"], "application/octet-stream")
        self.assertEqual(item["target_folder_id"], "synthetic-folder")

    def test_corporate_only_message_is_skipped_and_dual_label_is_scanned(self):
        data = synthetic_xlsx(report_text())
        broker_message = dict(self.message)
        broker_message["attachments"] = [attachment(data)]
        corporate_message = {
            "labels": [self.corporate_id],
            "headers": {"from": "project-address-04@example.invalid"},
            "attachments": [attachment(data, part_id="2", attachment_id="att-2")],
        }
        fetched = []

        def get_message(message_id):
            return corporate_message if message_id == "corporate-only" else broker_message

        result = self.scan(
            data,
            pages={"": {"messages": [{"id": "broker-and-corporate"},
                                      {"id": "corporate-only"}], "next_page_token": ""}},
            get_message=get_message,
            get_attachment=lambda mid, part, limit: fetched.append((mid, part)) or data,
        )
        self.assertEqual(result["result_counts"]["NEW"], 1)
        self.assertEqual(result["skipped_notification_only"], 1)
        self.assertEqual(len(fetched), 1)
        self.assertEqual(fetched[0][0], "broker-and-corporate")

    def test_all_three_categories_are_classified_from_attachment_contents(self):
        cases = (
            (BROKER, report_text()),
            (REPORT_CATEGORIES[1],
             "Выписка по счёту депо. Ценные бумаги, движение и остатки. "
             "Счёт депо № ACC12345."),
            (REPORT_CATEGORIES[2],
             "Отчёт о выплатах по ценным бумагам. Счёт № ACC12345. "
             "Купоны, выплаты и налоги."),
        )
        for index, (category, text) in enumerate(cases):
            with self.subTest(category=category):
                data = synthetic_xlsx(text)
                records = [archive_row(category=category)]
                result = self.scan(data, archive_records=records)
                self.assertEqual(result["result_counts"]["NEW"], 1)
                self.assertEqual(result["results"][0]["category"], category)

    def test_pdf_magic_uses_extracted_document_content(self):
        from unittest.mock import patch
        from msp_bonds_vtb_reports.gmail_discovery import extract_document_text

        data = b"%PDF-synthetic"
        message = dict(self.message)
        message["attachments"] = [attachment(
            data, filename="misleading.bin", mime="application/octet-stream"
        )]
        with patch(
            "msp_bonds_vtb_reports.gmail_discovery.extract_document_text",
            return_value=ExtractionResult("PDF", "OK", report_text()),
        ):
            result = self.scan(data, get_message=lambda _message_id: message)
        self.assertEqual(result["result_counts"]["NEW"], 1)
        self.assertEqual(result["results"][0]["category"], BROKER)

    def test_exact_sha256_match_is_already_archived(self):
        data = synthetic_xlsx(report_text())
        digest = hashlib.sha256(data).hexdigest()
        result = self.scan(data, archive_records=[archive_row(digest=digest)])
        self.assertEqual(result["result_counts"]["ALREADY_ARCHIVED"], 1)
        self.assertEqual(result["results"][0]["archive_refs"], ["V0001"])

    def test_verified_exact_hash_is_archived_even_when_folder_id_is_missing(self):
        data = synthetic_xlsx(report_text())
        record = archive_row(digest=hashlib.sha256(data).hexdigest())
        record.pop("drive_parent_folder_id")
        result = self.scan(data, archive_records=[record])
        self.assertEqual(result["result_counts"]["ALREADY_ARCHIVED"], 1)
        self.assertIn("ARCHIVED_LOCATION_NOT_VERIFIED",
                      result["results"][0]["reason_codes"])

    def test_same_type_account_and_period_different_bytes_needs_review(self):
        data = synthetic_xlsx(report_text())
        result = self.scan(
            data,
            archive_records=[archive_row(period_start="2026-03-01", period_end="2026-03-31")],
        )
        self.assertEqual(result["result_counts"]["NEEDS_REVIEW"], 1)
        self.assertIn("POSSIBLE_VERSION_CONFLICT", result["results"][0]["reason_codes"])

    def test_overlapping_period_is_not_a_duplicate(self):
        data = synthetic_xlsx(report_text())
        result = self.scan(
            data,
            archive_records=[archive_row(period_start="2026-03-15", period_end="2026-04-15")],
        )
        self.assertEqual(result["result_counts"]["NEW"], 1)
        self.assertIn("OVERLAPPING_PERIOD_IS_NOT_A_DUPLICATE",
                      result["results"][0]["reason_codes"])

    def test_untrusted_or_unconfigured_sender_is_not_downloaded(self):
        data = synthetic_xlsx(report_text())
        calls = []
        result = self.scan(
            data,
            allowed_senders={"project-address-02@example.invalid"},
            get_attachment=lambda *args: calls.append(args),
        )
        self.assertEqual(result["result_counts"]["NEEDS_REVIEW"], 1)
        self.assertIn("SENDER_NOT_ALLOWLISTED", result["results"][0]["reason_codes"])
        self.assertEqual(calls, [])

        result = self.scan(data, allowed_senders=(),
                           get_attachment=lambda *args: calls.append(args))
        self.assertIn("SENDER_ALLOWLIST_NOT_CONFIGURED", result["results"][0]["reason_codes"])
        self.assertEqual(calls, [])

    def test_oversized_attachment_is_rejected_before_fetch(self):
        data = b""
        meta = attachment(data, size=MAX_ATTACHMENT_BYTES + 1)
        calls = []
        result = self.scan(
            data,
            attachment_meta=meta,
            get_attachment=lambda *args: calls.append(args),
        )
        self.assertEqual(result["result_counts"]["NEEDS_REVIEW"], 1)
        self.assertIn("ATTACHMENT_SIZE_LIMIT_EXCEEDED",
                      result["results"][0]["reason_codes"])
        self.assertEqual(calls, [])

    def test_same_document_in_two_messages_is_not_reported_as_two_new_files(self):
        data = synthetic_xlsx(report_text())
        message_one = dict(self.message)
        message_one["attachments"] = [attachment(data, part_id="1", attachment_id="a1")]
        message_two = dict(message_one)
        message_two["attachments"] = [attachment(data, part_id="1", attachment_id="a2")]
        result = self.scan(
            data,
            pages={"": {"messages": [{"id": "msg-1"}, {"id": "msg-2"}],
                        "next_page_token": ""}},
            get_message=lambda message_id: (
                message_one if message_id == "msg-1" else message_two
            ),
        )
        self.assertEqual(result["result_counts"]["NEW"], 1)
        self.assertEqual(result["result_counts"]["NEEDS_REVIEW"], 1)
        self.assertIn("SAME_DOCUMENT_IN_MULTIPLE_MESSAGES_IN_THIS_SCAN",
                      result["results"][1]["reason_codes"])

    def test_message_error_can_be_retried_in_a_later_scan(self):
        data = synthetic_xlsx(report_text())
        attempts = {"count": 0}
        message = dict(self.message)
        message["attachments"] = [attachment(data)]

        def failing_once(_message_id):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("synthetic")
            return message

        first = self.scan(data, get_message=failing_once)
        second = self.scan(data, get_message=failing_once)
        self.assertEqual(first["result_counts"]["ERROR"], 1)
        self.assertEqual(second["result_counts"]["NEW"], 1)

    def test_incomplete_archive_registry_never_proves_new(self):
        data = synthetic_xlsx(report_text())
        message = dict(self.message)
        message["attachments"] = [attachment(data)]
        result = scan_gmail_reports(
            search_page=lambda **_kwargs: {"messages": [{"id": "msg-1"}]},
            get_message=lambda _message_id: message,
            get_attachment=lambda *_args: data,
            archive_records=[archive_row()],
            archive_complete=False,
            allowed_senders=self.allowlist,
            broker_label_id=BROKER_ID,
            corporate_action_label_id=self.corporate_id,
        )
        self.assertEqual(result["result_counts"]["NEEDS_REVIEW"], 1)
        self.assertIn("ARCHIVE_REGISTRY_UNVERIFIED",
                      result["results"][0]["reason_codes"])

    def test_invalid_document_bytes_require_review(self):
        data = b"not a PDF or workbook"
        result = self.scan(data)
        self.assertEqual(result["result_counts"]["NEEDS_REVIEW"], 1)
        self.assertIn("DOCUMENT_TEXT_UNAVAILABLE",
                      result["results"][0]["reason_codes"])

    def test_google_api_failure_is_error(self):
        result = scan_gmail_reports(
            search_page=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic")),
            get_message=lambda _message_id: self.message,
            get_attachment=lambda *_args: b"",
            archive_records=[archive_row()],
            archive_complete=True,
            allowed_senders=self.allowlist,
            broker_label_id=BROKER_ID,
            corporate_action_label_id=self.corporate_id,
            page_token="continuation",
        )
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["errors"][0]["reason_code"], "GMAIL_SEARCH_FAILED")
        self.assertEqual(result["next_page_token"], "")
