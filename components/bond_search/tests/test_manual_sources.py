from datetime import date, datetime, timedelta, timezone
import unittest

from msp_bonds_bond_search.catalog import PolicySourceSnapshot, RESOURCE_HEADERS, RULE_HEADERS
from msp_bonds_bond_search.evidence import EvidenceKind, EvidenceState, StageState, assess_stage
from msp_bonds_bond_search.manual_sources import (
    ISSUE_TERMS_CLAIMS,
    RATING_CLAIMS,
    capture_manual_issue_terms,
    capture_manual_rating,
)

NOW = datetime.now(timezone.utc)
RATING_SOURCE = "Банк России — рейтинговый репозитарий"
ISSUE_SOURCE = "НРД / NSD Data"


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


class ManualSourceCaptureTests(unittest.TestCase):
    def test_rating_capture_preserves_registered_source_but_never_confirms_values(self):
        values = {claim: f"synthetic-{claim}" for claim in RATING_CLAIMS}
        evidence = capture_manual_rating(
            values,
            snapshot=source_snapshot(),
            source_name=RATING_SOURCE,
            retrieved_at=NOW,
            document_date=date(2026, 11, 14),
            locator="synthetic page 1",
        )
        self.assertEqual(tuple(item.claim for item in evidence), RATING_CLAIMS)
        self.assertTrue(all(item.state is EvidenceState.UNVERIFIED for item in evidence))
        self.assertTrue(all(item.kind is EvidenceKind.OFFICIAL for item in evidence))
        self.assertTrue(all(item.source_url == "https://ratings.cbr.ru/" for item in evidence))
        result = assess_stage("rating", evidence)
        self.assertIs(result.state, StageState.NEEDS_REVIEW)
        self.assertEqual(set(result.missing_claims), set(RATING_CLAIMS))

    def test_missing_rating_fields_remain_missing_and_no_defaults_are_fabricated(self):
        evidence = capture_manual_rating(
            {"rating_grade": "synthetic-BBB"},
            snapshot=source_snapshot(),
            source_name=RATING_SOURCE,
            retrieved_at=NOW,
        )
        by_claim = {item.claim: item for item in evidence}
        self.assertIs(by_claim["rating_grade"].state, EvidenceState.UNVERIFIED)
        self.assertEqual(by_claim["rating_grade"].value, "synthetic-BBB")
        for claim in RATING_CLAIMS[1:]:
            self.assertIs(by_claim[claim].state, EvidenceState.MISSING)
            self.assertIsNone(by_claim[claim].value)

    def test_issue_terms_capture_does_not_make_incomplete_terms_verified(self):
        values = {claim: f"synthetic-{claim}" for claim in ISSUE_TERMS_CLAIMS[:2]}
        evidence = capture_manual_issue_terms(
            values,
            snapshot=source_snapshot(),
            source_name=ISSUE_SOURCE,
            retrieved_at=NOW,
            document_date=date(2026, 1, 1),
            locator="synthetic section 4",
        )
        by_claim = {item.claim: item for item in evidence}
        self.assertIs(by_claim["coupon_legal_terms"].state, EvidenceState.UNVERIFIED)
        self.assertIs(by_claim["coupon_schedule"].state, EvidenceState.UNVERIFIED)
        self.assertIs(by_claim["amortization_terms"].state, EvidenceState.MISSING)
        self.assertIs(by_claim["offer_status"].state, EvidenceState.MISSING)
        self.assertIs(by_claim["future_coupon_terms_legally_defined"].state, EvidenceState.MISSING)
        result = assess_stage("coupon_terms", evidence)
        self.assertIs(result.state, StageState.NEEDS_REVIEW)

    def test_unregistered_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not registered"):
            capture_manual_rating(
                {"rating_grade": "synthetic-BBB"},
                snapshot=source_snapshot(),
                source_name="Unregistered source",
                retrieved_at=NOW,
            )

    def test_source_registered_for_another_category_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not registered"):
            capture_manual_rating(
                {"rating_grade": "synthetic-BBB"},
                snapshot=source_snapshot(),
                source_name=ISSUE_SOURCE,
                retrieved_at=NOW,
            )

    def test_rating_status_labels_are_normalized_fail_closed(self):
        active = capture_manual_rating(
            {"rating_status": "U"}, snapshot=source_snapshot(),
            source_name=RATING_SOURCE, retrieved_at=NOW,
        )
        review = capture_manual_rating(
            {"rating_status": "W"}, snapshot=source_snapshot(),
            source_name=RATING_SOURCE, retrieved_at=NOW,
        )
        unknown = capture_manual_rating(
            {"rating_status": "synthetic unknown status"}, snapshot=source_snapshot(),
            source_name=RATING_SOURCE, retrieved_at=NOW,
        )
        self.assertEqual(next(item for item in active if item.claim == "rating_status").value, "ACTIVE")
        self.assertEqual(next(item for item in review if item.claim == "rating_status").value, "UNDER_REVIEW")
        self.assertEqual(next(item for item in unknown if item.claim == "rating_status").value, "UNKNOWN")

    def test_stale_source_register_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "current policy/source snapshot"):
            capture_manual_rating(
                {"rating_grade": "synthetic-BBB"},
                snapshot=source_snapshot(NOW - timedelta(hours=2)),
                source_name=RATING_SOURCE,
                retrieved_at=NOW,
            )

    def test_typoed_claim_is_rejected_instead_of_ignored(self):
        with self.assertRaises(ValueError):
            capture_manual_rating(
                {"rating_gradee": "synthetic-BBB"},
                snapshot=source_snapshot(),
                source_name=RATING_SOURCE,
                retrieved_at=NOW,
            )


if __name__ == "__main__":
    unittest.main()
