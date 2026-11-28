"""Synthetic offline tests for extraction, content classification and label policy."""
from __future__ import annotations

import io
import unittest
from unittest.mock import Mock, patch
from zipfile import ZIP_DEFLATED, ZipFile

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from msp_bonds_vtb_reports.classifier import (
    BROKER_REPORT,
    DEPOT_STATEMENT,
    SECURITIES_PAYMENTS,
    classify_document,
)
from msp_bonds_vtb_reports.mail_routing import route_message_labels
from msp_bonds_vtb_reports.text_extraction import extract_document_text


def synthetic_xlsx(text: str) -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            "<worksheet><sheetData><row><c r='A1' t='inlineStr'><is><t>"
            + text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            + "</t></is></c></row></sheetData></worksheet>",
        )
    return buffer.getvalue()


def synthetic_xlsx_with_numeric_dates() -> bytes:
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook><workbookPr date1904='0'/></workbook>")
        archive.writestr(
            "xl/styles.xml",
            "<styleSheet><numFmts count='1'><numFmt numFmtId='164' formatCode='dd.mm.yyyy'/></numFmts>"
            "<cellXfs count='2'><xf numFmtId='0'/><xf numFmtId='164' applyNumberFormat='1'/></cellXfs></styleSheet>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            "<worksheet><sheetData><row>"
            "<c r='A1' t='inlineStr'><is><t>Период операций: с</t></is></c>"
            "<c r='B1' s='1'><v>46023</v></c>"
            "<c r='C1' t='inlineStr'><is><t>по</t></is></c>"
            "<c r='D1' s='1'><v>46053</v></c>"
            "</row><row>"
            "<c r='A2' t='inlineStr'><is><t>Брокерский счёт №</t></is></c>"
            "<c r='B2'><v>407028</v></c>"
            "</row></sheetData></worksheet>",
        )
    return buffer.getvalue()


def synthetic_pdf(text: str | None = None) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    if text is not None:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        font_ref = writer._add_object(font)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        stream = DecodedStreamObject()
        safe_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 20 250 Td ({safe_text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class ExtractionAndClassificationTests(unittest.TestCase):
    def test_three_categories_use_document_content(self):
        cases = (
            (BROKER_REPORT, "Брокерский отчёт. Счёт № 40702810000000000001. Отчётный период: 01.01.2026 — 31.01.2026."),
            (DEPOT_STATEMENT, "Выписка по счёту депо. Субсчёт № 40702810000000000002. Дата отчёта: 31.01.2026."),
            (SECURITIES_PAYMENTS, "Отчёт о выплатах по ценным бумагам. Счёт № 40702810000000000003. Купоны и налоги."),
        )
        for category, text in cases:
            with self.subTest(category=category):
                result = classify_document(
                    text, text.encode("utf-8"), filename="unrelated.txt",
                    mime_type="application/octet-stream", subject="Уведомление",
                )
                self.assertEqual(result.category, category)
                self.assertEqual(result.status, "CLASSIFIED")
                self.assertEqual(len(result.sha256), 64)

    def test_broker_trade_evidence_overrides_incidental_depot_and_coupon_terms(self):
        text = (
            "Отчёт. Сделки. Денежные операции и остатки. Счёт депо, ценные бумаги, "
            "движение, купоны, выплаты и налоги. Брокерский счёт № 40702810000000000011."
        )
        result = classify_document(text, text.encode("utf-8"))
        self.assertEqual(result.category, BROKER_REPORT)
        self.assertEqual(result.status, "CLASSIFIED")

    def test_corporate_action_notification_is_not_a_broker_report(self):
        text = "Уведомление о корпоративном действии по ценным бумагам. Счёт № 40702810000000000004."
        result = classify_document(text, text.encode("utf-8"))
        self.assertIsNone(result.category)
        self.assertEqual(result.status, "NEEDS_REVIEW")
        self.assertIn("CORPORATE_ACTION_NOTIFICATION", result.reasons)

    def test_ambiguous_or_unknown_text_needs_review(self):
        result = classify_document("Справка о состоянии портфеля. Счёт № 40702810000000000005.", b"synthetic")
        self.assertIsNone(result.category)
        self.assertEqual(result.status, "NEEDS_REVIEW")
        extraction = extract_document_text(b"not a PDF or spreadsheet", filename="statement.pdf", mime_type="application/pdf")
        self.assertEqual(extraction.status, "UNSUPPORTED")
        self.assertEqual(extraction.detected_format, "UNKNOWN")

    def test_wrong_extension_and_octet_stream_do_not_override_xlsx_bytes(self):
        text = "Брокерский отчёт. Счёт № 40702810000000000006."
        data = synthetic_xlsx(text)
        extracted = extract_document_text(data, filename="misleading.pdf", mime_type="application/octet-stream")
        self.assertEqual(extracted.detected_format, "XLSX")
        self.assertEqual(extracted.status, "OK")
        result = classify_document(extracted.text, data, filename="misleading.pdf", mime_type="application/octet-stream")
        self.assertEqual(result.category, BROKER_REPORT)
        self.assertEqual(result.status, "CLASSIFIED")

    def test_numeric_xlsx_dates_and_account_cells_are_extracted(self):
        data = synthetic_xlsx_with_numeric_dates()
        extracted = extract_document_text(data)
        self.assertEqual(extracted.status, "OK")
        self.assertIn("2026-01-01", extracted.text)
        self.assertIn("2026-01-31", extracted.text)
        result = classify_document(extracted.text, data)
        self.assertEqual(result.period_start, "2026-01-01")
        self.assertEqual(result.period_end, "2026-01-31")
        self.assertTrue(result.account_ids)

    def test_pdf_text_layer_is_extracted(self):
        data = synthetic_pdf("Synthetic PDF text layer")
        extracted = extract_document_text(data, filename="unrelated.xlsx", mime_type="application/octet-stream")
        self.assertEqual(extracted.detected_format, "PDF")
        self.assertEqual(extracted.status, "OK")
        self.assertEqual(extracted.pages, 1)
        self.assertIn("Synthetic PDF text layer", extracted.text)

    def test_pdf_plain_extraction_preserves_a_multiline_account_field(self):
        plain_text = (
            "Отчёт о выплатах по ценным бумагам. Номер счета депо:\n"
            "A12BC34567\nКупоны, выплаты и налоги."
        )
        page = Mock()
        page.get_contents.return_value = None
        page.extract_text.return_value = plain_text
        reader = Mock()
        reader.is_encrypted = False
        reader.pages = [page]
        with patch("pypdf.PdfReader", return_value=reader):
            extracted = extract_document_text(b"%PDF-synthetic")
        result = classify_document(extracted.text, b"%PDF-synthetic")
        self.assertEqual(extracted.status, "OK")
        self.assertEqual(result.status, "CLASSIFIED")
        self.assertEqual(result.account_ids, ("A12BC34567",))
        page.extract_text.assert_called_once_with()

    def test_pdf_layout_extraction_remains_a_fallback(self):
        page = Mock()
        page.get_contents.return_value = None
        page.extract_text.side_effect = ["", "Fallback PDF text"]
        reader = Mock()
        reader.is_encrypted = False
        reader.pages = [page]
        with patch("pypdf.PdfReader", return_value=reader):
            extracted = extract_document_text(b"%PDF-synthetic")
        self.assertEqual(extracted.status, "OK")
        self.assertEqual(extracted.text, "Fallback PDF text")
        self.assertEqual(page.extract_text.call_count, 2)

    def test_pdf_without_text_layer_needs_review(self):
        extracted = extract_document_text(synthetic_pdf())
        self.assertEqual(extracted.detected_format, "PDF")
        self.assertEqual(extracted.status, "NEEDS_REVIEW")
        self.assertIsNone(extracted.text)

    def test_missing_account_requires_review(self):
        text = "Брокерский отчёт. Отчётный период: 01.01.2026 — 31.01.2026."
        result = classify_document(text, text.encode("utf-8"))
        self.assertEqual(result.category, BROKER_REPORT)
        self.assertEqual(result.status, "NEEDS_REVIEW")
        self.assertIn("ACCOUNT_NOT_FOUND", result.reasons)

    def test_multiple_accounts_are_kept_without_a_fixed_limit(self):
        ids = [f"407028100000000000{i:02d}" for i in range(12)]
        text = "Брокерский отчёт. " + " ".join(f"Счёт № {value}." for value in ids)
        result = classify_document(text, text.encode("utf-8"))
        self.assertEqual(result.status, "CLASSIFIED")
        self.assertEqual(len(result.account_ids), 12)
        self.assertEqual(set(result.account_ids), set(ids))

    def test_explicit_subaccount_field_wins_over_incidental_account_mentions(self):
        text = (
            "Брокерский отчёт. № субсчёта AB123. "
            "В комментарии операции указан счёт № XY456."
        )
        result = classify_document(text, text.encode("utf-8"))
        self.assertEqual(result.status, "CLASSIFIED")
        self.assertEqual(result.account_ids, ("AB123",))

    def test_unreadable_primary_account_field_does_not_fall_back_to_incidental_id(self):
        text = (
            "Брокерский отчёт. № субсчёта не указан. "
            "В комментарии операции указан счёт № XY456."
        )
        result = classify_document(text, text.encode("utf-8"))
        self.assertEqual(result.status, "NEEDS_REVIEW")
        self.assertEqual(result.account_ids, ())
        self.assertIn("ACCOUNT_NOT_FOUND", result.reasons)

    def test_period_report_and_generated_dates_remain_separate(self):
        text = (
            "Брокерский отчёт. Счёт № 40702810000000000007. "
            "Период операций: с 01.01.2026 по 31.01.2026. "
            "Отчётная дата: 31.01.2026. Дата формирования: 02.02.2026."
        )
        result = classify_document(text, text.encode("utf-8"))
        self.assertEqual(result.period_start, "2026-01-01")
        self.assertEqual(result.period_end, "2026-01-31")
        self.assertEqual(result.report_date, "2026-01-31")
        self.assertEqual(result.generated_date, "2026-02-02")

    def test_two_gmail_labels_route_through_content_check(self):
        route = route_message_labels(
            {"Label_Broker", "Label_Corporate"},
            broker_label_id="Label_Broker",
            corporate_action_label_id="Label_Corporate",
        )
        self.assertEqual(route.action, "SCAN_CANDIDATE")
        self.assertTrue(route.has_corporate_action_label)
        self.assertIn("classify attachment content", route.reason)
        notification_only = route_message_labels(
            {"Label_Corporate"},
            broker_label_id="Label_Broker",
            corporate_action_label_id="Label_Corporate",
        )
        self.assertEqual(notification_only.action, "SKIP_NOTIFICATION_ONLY")


if __name__ == "__main__":
    unittest.main()
