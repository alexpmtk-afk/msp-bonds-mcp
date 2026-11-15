from datetime import date, datetime, timezone
import unittest

from msp_bonds_bond_search.evidence import EvidenceKind, EvidenceState, StageState, assess_stage
from msp_bonds_bond_search.manual_sources import (
    ISSUE_TERMS_CLAIMS,
    RATING_CLAIMS,
    capture_manual_issue_terms,
    capture_manual_rating,
)

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


class ManualSourceCaptureTests(unittest.TestCase):
    def test_rating_capture_preserves_source_but_never_confirms_values(self):
        values = {claim: f"synthetic-{claim}" for claim in RATING_CLAIMS}
        evidence = capture_manual_rating(
            values,
            source_name="Synthetic official rating record",
            source_url="https://ratings.example/record",
            retrieved_at=NOW,
            document_date=date(2026, 11, 14),
            locator="synthetic page 1",
        )
        self.assertEqual(tuple(item.claim for item in evidence), RATING_CLAIMS)
        self.assertTrue(all(item.state is EvidenceState.UNVERIFIED for item in evidence))
        self.assertTrue(all(item.kind is EvidenceKind.OFFICIAL for item in evidence))
        result = assess_stage("rating", evidence)
        self.assertIs(result.state, StageState.NEEDS_REVIEW)
        self.assertEqual(set(result.missing_claims), set(RATING_CLAIMS))

    def test_missing_rating_fields_remain_missing_and_no_defaults_are_fabricated(self):
        evidence = capture_manual_rating(
            {"rating_grade": "synthetic-BBB"},
            source_name="Synthetic official rating record",
            source_url="https://ratings.example/record",
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
            source_name="Synthetic issue decision",
            source_url="https://issuer.example/issue-decision",
            retrieved_at=NOW,
            document_date=date(2026, 1, 1),
            locator="synthetic section 4",
        )
        by_claim = {item.claim: item for item in evidence}
        self.assertIs(by_claim["coupon_legal_terms"].state, EvidenceState.UNVERIFIED)
        self.assertIs(by_claim["coupon_schedule"].state, EvidenceState.UNVERIFIED)
        self.assertIs(by_claim["amortization_terms"].state, EvidenceState.MISSING)
        self.assertIs(by_claim["offer_status"].state, EvidenceState.MISSING)
        result = assess_stage("coupon_terms", evidence)
        self.assertIs(result.state, StageState.NEEDS_REVIEW)

    def test_typoed_claim_is_rejected_instead_of_ignored(self):
        with self.assertRaises(ValueError):
            capture_manual_rating(
                {"rating_gradee": "synthetic-BBB"},
                source_name="Synthetic record",
                source_url="https://ratings.example/record",
                retrieved_at=NOW,
            )


if __name__ == "__main__":
    unittest.main()
