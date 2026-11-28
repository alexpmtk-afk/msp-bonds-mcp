"""Synthetic tests for metadata-only archive reconciliation."""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from msp_bonds_vtb_reports.archive import REPORT_CATEGORIES
from msp_bonds_vtb_reports.archive_reconciliation import (
    account_set_route_key,
    build_folder_route_index,
    match_archive_candidate,
    reconcile_archive,
    resolve_target_folder,
    write_archive_registry,
)
from msp_bonds_vtb_reports.classifier import classify_document


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


class ArchiveReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "archive"
        self.broker = REPORT_CATEGORIES[0]
        self.account_path = self.root / self.broker / "synthetic-account"
        self.account_path.mkdir(parents=True)
        self.base_text = (
            "Брокерский отчёт. Счёт № 40702810000000000001. "
            "Период операций: с 01.01.2026 по 31.01.2026."
        )
        self.base_bytes = synthetic_xlsx(self.base_text)
        (self.account_path / "original.xlsx").write_bytes(self.base_bytes)
        duplicate_dir = self.root / self.broker / "another-synthetic-folder"
        duplicate_dir.mkdir()
        (duplicate_dir / "same-content.xlsx").write_bytes(self.base_bytes)
        self.version_text = self.base_text + " Обновлённая версия документа."
        self.version_bytes = synthetic_xlsx(self.version_text)
        (self.account_path / "changed-bytes.xlsx").write_bytes(self.version_bytes)
        self.overlap_text = (
            "Брокерский отчёт. Счёт № 40702810000000000001. "
            "Период операций: с 15.01.2026 по 15.02.2026."
        )
        self.overlap_bytes = synthetic_xlsx(self.overlap_text)
        (self.account_path / "overlap.xlsx").write_bytes(self.overlap_bytes)

    def _drive_metadata(self):
        result = {}
        for path in self.root.rglob("*.xlsx"):
            raw = path.read_bytes()
            rel = path.relative_to(self.root).as_posix()
            result[rel] = {
                "id": "synthetic-drive-" + path.stem,
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "drive_parent_folder_id": "synthetic-folder-" + path.parent.name,
            }
        return result

    def test_exact_hash_versions_and_overlaps_are_distinct(self):
        registry = reconcile_archive(self.root, self._drive_metadata())
        self.assertEqual(registry["status"], "OK")
        self.assertEqual(registry["total"], 4)
        self.assertEqual(registry["drive_verification_counts"]["MATCH"], 4)
        self.assertEqual(registry["classification_counts"]["PASS"], 4)
        self.assertTrue(all(row["drive_parent_folder_id"] for row in registry["records"]))
        exact_pairs = [row for row in registry["records"] if row["exact_hash_duplicate_refs"]]
        self.assertEqual(len(exact_pairs), 2)
        version_rows = [row for row in registry["records"] if row["same_metadata_different_bytes"]]
        self.assertEqual(len(version_rows), 3)
        overlapping_rows = [row for row in registry["records"] if row["overlapping_period_refs"]]
        self.assertEqual(len(overlapping_rows), 3)

    def test_candidate_matching_uses_hash_then_metadata(self):
        registry = reconcile_archive(self.root, self._drive_metadata())
        records = registry["records"]
        exact = classify_document(self.base_text, self.base_bytes)
        exact_match = match_archive_candidate(exact, records)
        self.assertEqual(exact_match["status"], "EXACT_DUPLICATE")
        self.assertEqual(len(exact_match["drive_file_ids"]), 2)

        unseen_version_text = self.base_text + " A third synthetic version."
        unseen_version = classify_document(unseen_version_text, synthetic_xlsx(unseen_version_text))
        self.assertEqual(match_archive_candidate(unseen_version, records)["status"], "POSSIBLE_VERSION")

        overlap = classify_document(self.overlap_text, self.overlap_bytes)
        # The exact overlap file exists in the archive, so exact hash wins first.
        self.assertEqual(match_archive_candidate(overlap, records)["status"], "EXACT_DUPLICATE")
        unseen_overlap_text = self.overlap_text.replace("15.01.2026", "20.01.2026").replace("15.02.2026", "20.02.2026")
        changed_overlap = classify_document(unseen_overlap_text, synthetic_xlsx(unseen_overlap_text))
        overlap_match = match_archive_candidate(changed_overlap, records)
        self.assertEqual(overlap_match["status"], "OVERLAPPING_PERIOD")
        self.assertNotIn(overlap_match["status"], {"EXACT_DUPLICATE", "POSSIBLE_VERSION"})

    def test_distinct_primary_subaccounts_are_not_possible_versions(self):
        first_text = (
            "Брокерский отчёт. № субсчёта AB123. "
            "Период операций: с 01.01.2026 по 31.01.2026. "
            "В комментарии указан счёт № ZZ999."
        )
        second_text = first_text.replace("AB123", "CD456")
        first = classify_document(first_text, synthetic_xlsx(first_text))
        second = classify_document(second_text, synthetic_xlsx(second_text))
        self.assertEqual(first.account_ids, ("AB123",))
        self.assertEqual(second.account_ids, ("CD456",))
        record = {
            "sha256": first.sha256,
            "drive_verification": "MATCH",
            "drive_file_id": "synthetic-drive-first",
            "detected_category": first.category,
            "account_ids": list(first.account_ids),
            "period_start": first.period_start,
            "period_end": first.period_end,
            "archive_ref": "synthetic-first",
        }
        self.assertEqual(match_archive_candidate(second, [record])["status"], "NEW_DOCUMENT")

    def _verified_route_record(self, result, folder_id, drive_file_id="synthetic-drive-route", verification="MATCH"):
        return {
            "archive_ref": "synthetic-route",
            "sha256": result.sha256,
            "drive_file_id": drive_file_id,
            "drive_parent_folder_id": folder_id,
            "drive_verification": verification,
            "classification_status": result.status,
            "classification_outcome": "PASS" if result.status == "CLASSIFIED" else "NEEDS_REVIEW",
            "detected_category": result.category,
            "account_ids": list(result.account_ids),
        }

    def test_target_folder_uses_sha256_before_classification_or_routing(self):
        registry = reconcile_archive(self.root, self._drive_metadata())
        incomplete = classify_document("Unrecognized document", self.base_bytes)
        decision = resolve_target_folder(incomplete, registry["records"])
        self.assertEqual(decision["status"], "EXACT_DUPLICATE")
        self.assertEqual(decision["reason"], "SHA256_ALREADY_IN_ARCHIVE")
        self.assertEqual(len(decision["target_folder_ids"]), 2)
        self.assertIsNone(decision["target_folder_id"])

    def test_confirmed_account_set_routes_to_one_existing_folder(self):
        known = classify_document(self.base_text, self.base_bytes)
        new_text = self.base_text + " Дополнительное пояснение."
        candidate = classify_document(new_text, synthetic_xlsx(new_text))
        record = self._verified_route_record(known, "synthetic-folder-one")
        decision = resolve_target_folder(candidate, [record])
        self.assertEqual(decision["status"], "READY")
        self.assertEqual(decision["target_folder_id"], "synthetic-folder-one")

    def test_unknown_or_ambiguous_account_sets_need_review(self):
        known = classify_document(self.base_text, self.base_bytes)
        candidate_text = self.overlap_text + " Дополнительное пояснение."
        candidate = classify_document(candidate_text, synthetic_xlsx(candidate_text))
        first = self._verified_route_record(known, "synthetic-folder-one", "synthetic-drive-one")
        second_text = self.version_text
        second = classify_document(second_text, self.version_bytes)
        other_folder = self._verified_route_record(second, "synthetic-folder-two", "synthetic-drive-two")
        ambiguous = resolve_target_folder(candidate, [first, other_folder])
        self.assertEqual(ambiguous["status"], "NEEDS_REVIEW")
        self.assertEqual(ambiguous["reason"], "ACCOUNT_SET_EXISTS_IN_MULTIPLE_FOLDERS")
        self.assertEqual(len(ambiguous["target_folder_ids"]), 2)

        unknown_text = (
            "Брокерский отчёт. Счёт № 40702810000000009999. "
            "Период операций: с 01.01.2026 по 31.01.2026."
        )
        unknown = classify_document(unknown_text, synthetic_xlsx(unknown_text))
        unresolved = resolve_target_folder(unknown, [first])
        self.assertEqual(unresolved["status"], "NEEDS_REVIEW")
        self.assertEqual(unresolved["reason"], "NO_CONFIRMED_ACCOUNT_SET_ROUTE")
        self.assertEqual(unresolved["target_folder_ids"], [])

    def test_unverified_drive_folder_is_not_used_as_a_route(self):
        known = classify_document(self.base_text, self.base_bytes)
        new_text = self.base_text + " Изменённое пояснение."
        candidate = classify_document(new_text, synthetic_xlsx(new_text))
        record = self._verified_route_record(
            known, "unverified-folder", verification="UNVERIFIED"
        )
        decision = resolve_target_folder(candidate, [record])
        self.assertEqual(decision["status"], "NEEDS_REVIEW")
        self.assertEqual(decision["reason"], "NO_CONFIRMED_ACCOUNT_SET_ROUTE")

    def test_account_set_route_key_ignores_order_and_case(self):
        first = account_set_route_key(self.broker, ["AB123", "CD456"])
        second = account_set_route_key(self.broker.swapcase(), ["cd456", "ab123", "AB123"])
        self.assertEqual(first, second)

    def test_incomplete_candidate_metadata_requires_review(self):
        text = "Брокерский отчёт. Отчётный период: 01.01.2026 — 31.01.2026."
        candidate = classify_document(text, text.encode("utf-8"))
        result = match_archive_candidate(candidate, [])
        self.assertEqual(result["status"], "NEEDS_REVIEW")

    def test_drive_mismatch_is_not_claimed_as_verified(self):
        path = next(self.root.rglob("original.xlsx"))
        raw = path.read_bytes()
        relative = path.relative_to(self.root).as_posix()
        registry = reconcile_archive(self.root, {relative: {"id": "synthetic-id", "size": len(raw), "sha256": "0" * 64}})
        record = next(row for row in registry["records"] if row["relative_path"] == relative)
        self.assertEqual(record["drive_verification"], "MISMATCH")
        self.assertEqual(registry["status"], "PARTIAL")

    def test_private_registry_persists_metadata_not_document_bytes_or_text(self):
        registry = reconcile_archive(self.root, self._drive_metadata())
        target = Path(self.temporary.name) / "private" / "registry.json"
        write_archive_registry(target, registry)
        payload = target.read_text(encoding="utf-8")
        parsed = json.loads(payload)
        self.assertEqual(parsed["total"], 4)
        self.assertNotIn(self.base_text, payload)
        self.assertNotIn(self.base_bytes.decode("latin1"), payload)
        self.assertTrue(all("sha256" in row and "drive_file_id" in row for row in parsed["records"]))


if __name__ == "__main__":
    unittest.main()
