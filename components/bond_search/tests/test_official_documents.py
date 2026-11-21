from datetime import date, datetime, timedelta, timezone
import hashlib
import unittest

from msp_bonds_bond_search.catalog import PolicySourceSnapshot, RESOURCE_HEADERS, RULE_HEADERS
from msp_bonds_bond_search.evidence import EvidenceState, StageState, assess_stage
from msp_bonds_bond_search.official_documents import (
    DocumentCaptureStatus,
    capture_official_document,
)
from msp_bonds_bond_search.manual_sources import RATING_CLAIMS

NOW = datetime.now(timezone.utc)
ISSUE_SOURCE = "НРД / NSD Data"
RATING_SOURCE = "Банк России — рейтинговый репозитарий"


def source_snapshot(fetched_at=NOW):
    rules = [
        list(RULE_HEADERS),
        ["1", "Test", "Synthetic approved rule", "", "", "УТВЕРЖДЕНО", ""],
    ]
    resources = [
        list(RESOURCE_HEADERS),
        ["Official", RATING_SOURCE, "https://ratings.cbr.ru/", "Synthetic rating source", "Test only", "TEST", "rating"],
        ["Primary", ISSUE_SOURCE, "https://nsddata.ru/", "Synthetic issue source", "Test only", "TEST", "coupon_terms"],
    ]
    return PolicySourceSnapshot.from_sheet_rows(rules, resources, fetched_at=fetched_at)


def issue_capture(**overrides):
    args = {
        "snapshot": source_snapshot(),
        "source_name": ISSUE_SOURCE,
        "document_type": "ISSUE_DECISION",
        "document_id": "synthetic-document-001",
        "issuer": "Synthetic Issuer LLC",
        "isin": "RU000A123456",
        "issue_registration_number": "4B0000000000S001P",
        "document_date": date(2026, 11, 15),
        "retrieved_at": NOW,
        "artifact_bytes": b"synthetic issue decision bytes",
        "expected_isin": "RU000A123456",
        "expected_issuer": "Synthetic Issuer LLC",
        "expected_issue_registration_number": "4B0000000000S001P",
        "document_url": "https://nsddata.ru/issues/synthetic.pdf",
        "locator": "section 4, page 7",
        "values": {
            "coupon_legal_terms": "synthetic fixed coupon terms",
            "future_coupon_terms_legally_defined": True,
        },
        "field_locators": {
            "coupon_legal_terms": "section 4, page 7",
            "future_coupon_terms_legally_defined": "section 4, page 7",
        },
        "source_status": "CURRENT",
    }
    args.update(overrides)
    return capture_official_document(**args)


class OfficialDocumentCaptureTests(unittest.TestCase):
    def test_supplied_document_is_hashed_and_claims_remain_unverified(self):
        result = issue_capture()
        self.assertIs(result.status, DocumentCaptureStatus.CAPTURED_REVIEW_REQUIRED)
        self.assertEqual(result.provenance.isin, "RU000A123456")
        self.assertEqual(result.provenance.issue_registration_number, "4B0000000000S001P")
        self.assertEqual(result.provenance.issuer, "Synthetic Issuer LLC")
        self.assertEqual(result.provenance.artifact_sha256, hashlib.sha256(b"synthetic issue decision bytes").hexdigest())
        by_claim = {item.claim: item for item in result.evidence}
        self.assertIs(by_claim["coupon_legal_terms"].state, EvidenceState.UNVERIFIED)
        self.assertIs(by_claim["coupon_schedule"].state, EvidenceState.MISSING)
        self.assertIs(by_claim["future_coupon_terms_legally_defined"].state, EvidenceState.UNVERIFIED)
        self.assertEqual(by_claim["coupon_legal_terms"].document_provenance, result.provenance)

    def test_document_found_is_distinct_from_verified_terms(self):
        result = issue_capture()
        self.assertEqual(result.status.value, "DOCUMENT_CAPTURED_REVIEW_REQUIRED")
        self.assertTrue(all(item.state is not EvidenceState.CONFIRMED for item in result.evidence))
        stage = assess_stage("coupon_terms", result.evidence)
        self.assertIs(stage.state, StageState.NEEDS_REVIEW)

    def test_official_rating_record_captures_exact_scope_but_stays_unverified(self):
        values = {claim: f"synthetic-{claim}" for claim in RATING_CLAIMS}
        values.update({
            "rating_subject_type": "ISSUE",
            "rating_subject_id": "RU000A123456",
            "rating_status": "ACTIVE",
            "rating_action_is_latest": True,
        })
        locators = {claim: "synthetic page 1" for claim, value in values.items() if value is not None}
        result = capture_official_document(
            snapshot=source_snapshot(),
            source_name=RATING_SOURCE,
            document_type="RATING_RELEASE",
            document_id="synthetic-rating-action-001",
            issuer="Synthetic Issuer LLC",
            isin="RU000A123456",
            issue_registration_number="4B0000000000S001P",
            document_date=date(2026, 11, 15),
            retrieved_at=NOW,
            artifact_bytes=b"synthetic official rating release",
            expected_isin="RU000A123456",
            expected_issuer="Synthetic Issuer LLC",
            document_url="https://ratings.cbr.ru/records/synthetic",
            values=values,
            field_locators=locators,
            source_status="CURRENT",
        )
        self.assertIs(result.status, DocumentCaptureStatus.CAPTURED_REVIEW_REQUIRED)
        self.assertEqual(len(result.evidence), len(RATING_CLAIMS))
        self.assertTrue(all(item.state is EvidenceState.UNVERIFIED for item in result.evidence))

    def test_unknown_future_coupon_terms_remain_missing(self):
        result = issue_capture(values={"coupon_legal_terms": "synthetic terms"},
                               field_locators={"coupon_legal_terms": "section 4"})
        by_claim = {item.claim: item for item in result.evidence}
        self.assertIs(by_claim["future_coupon_terms_legally_defined"].state, EvidenceState.MISSING)

    def test_identity_mismatch_emits_no_claim_evidence(self):
        result = issue_capture(isin="RU000A654321")
        self.assertIs(result.status, DocumentCaptureStatus.IDENTITY_MISMATCH)
        self.assertEqual(result.evidence, ())

    def test_superseded_document_emits_no_claim_evidence(self):
        result = issue_capture(source_status="SUPERSEDED")
        self.assertIs(result.status, DocumentCaptureStatus.NOT_CURRENT)
        self.assertEqual(result.evidence, ())

    def test_unclassified_document_currentness_never_becomes_verified(self):
        result = issue_capture(source_status="UNKNOWN")
        self.assertIs(result.status, DocumentCaptureStatus.CAPTURED_CURRENTNESS_UNKNOWN)
        self.assertTrue(all(item.state is EvidenceState.UNVERIFIED or item.state is EvidenceState.MISSING
                            for item in result.evidence))

    def test_document_url_must_match_registered_official_host(self):
        with self.assertRaisesRegex(ValueError, "host differs"):
            issue_capture(document_url="https://unregistered.example/issue.pdf")

    def test_document_requires_payload_bytes(self):
        with self.assertRaisesRegex(ValueError, "bytes are required"):
            issue_capture(artifact_bytes=b"")

    def test_stale_source_snapshot_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "current policy/source snapshot"):
            issue_capture(snapshot=source_snapshot(NOW - timedelta(hours=2)))

    def test_rating_source_cannot_be_used_for_issue_terms_document(self):
        with self.assertRaisesRegex(ValueError, "not registered"):
            issue_capture(source_name=RATING_SOURCE)

    def test_document_date_cannot_be_in_the_future(self):
        with self.assertRaisesRegex(ValueError, "future"):
            issue_capture(document_date=date(2099, 1, 1))


if __name__ == "__main__":
    unittest.main()
