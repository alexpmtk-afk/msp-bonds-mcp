import unittest
from datetime import date, datetime, timezone
from dataclasses import replace

from msp_bonds_bond_search.evidence import (
    EvidenceItem,
    EvidenceKind,
    DocumentProvenance,
    EvidenceState,
    SequentialEvidenceVerifier,
    StageState,
    STAGE_ORDER,
    REQUIRED_CLAIMS,
    assess_stage,
)

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


def items_for(stage):
    result = []
    document = None
    if stage in {"rating", "coupon_terms"}:
        document = DocumentProvenance(
            document_type="RATING_RELEASE" if stage == "rating" else "ISSUE_DECISION",
            document_id="synthetic-doc-001",
            issuer="Synthetic Issuer LLC",
            isin="RU000A123456",
            issue_registration_number="4B0000000000S001P",
            document_date=date(2026, 11, 14),
            source_name="Synthetic official record",
            source_url="https://issuer.example/document",
            retrieved_at=NOW,
            artifact_sha256="a" * 64,
            source_status="CURRENT",
        )
    for claim in REQUIRED_CLAIMS[stage]:
        kind = EvidenceKind.CALCULATION if stage == "yield" and claim in {"full_entry_cost", "xirr"} else EvidenceKind.PRIMARY
        value = f"synthetic-{claim}"
        if stage == "rating":
            value = {
                "rating_subject_type": "ISSUE",
                "rating_subject_id": "RU000A123456",
                "rating_status": "ACTIVE",
                "rating_action_is_latest": True,
            }.get(claim, value)
        elif stage == "coupon_terms":
            value = {
                "future_coupon_terms_legally_defined": True,
                "issue_document_is_current": True,
            }.get(claim, value)
        result.append(EvidenceItem(
            claim=claim,
            value=value,
            unit="synthetic",
            source_name="Synthetic official record",
            source_url="https://issuer.example/document",
            kind=kind,
            state=EvidenceState.CONFIRMED,
            retrieved_at=NOW,
            document_date=date(2026, 11, 14),
            method="offline test fixture",
            document_provenance=document,
        ))
    return result


class EvidenceTests(unittest.TestCase):
    def test_all_required_claims_need_confirmed_primary_or_calculated_evidence(self):
        items = items_for("identity")
        result = assess_stage("identity", items)
        self.assertIs(result.state, StageState.VERIFIED)
        self.assertFalse(result.missing_claims)

    def test_unverified_secondary_data_does_not_satisfy_required_claim(self):
        items = items_for("rating")
        items[0] = EvidenceItem(
            claim=items[0].claim,
            value="BBB",
            unit="grade",
            source_name="Aggregator",
            source_url="https://aggregator.example/rating",
            kind=EvidenceKind.SECONDARY,
            state=EvidenceState.UNVERIFIED,
            retrieved_at=NOW,
        )
        result = assess_stage("rating", items)
        self.assertIs(result.state, StageState.NEEDS_REVIEW)
        self.assertIn("rating_grade", result.missing_claims)

    def test_conflicting_confirmed_sources_stop_stage(self):
        items = items_for("identity")
        original = items[0]
        items.append(EvidenceItem(
            claim=original.claim,
            value="different",
            unit=original.unit,
            source_name="Second primary record",
            source_url="https://registry.example/document",
            kind=EvidenceKind.OFFICIAL,
            state=EvidenceState.CONFIRMED,
            retrieved_at=NOW,
        ))
        result = assess_stage("identity", items)
        self.assertIs(result.state, StageState.CONFLICT)
        self.assertIn(original.claim, result.conflicting_claims)

    def test_explicit_conflict_overrides_a_confirmed_value(self):
        items = items_for("identity")
        original = items[0]
        items.append(EvidenceItem(
            claim=original.claim,
            value="contradictory synthetic value",
            unit=original.unit,
            source_name="Conflicting official record",
            source_url="https://registry.example/record",
            kind=EvidenceKind.OFFICIAL,
            state=EvidenceState.CONFLICT,
            retrieved_at=NOW,
        ))
        result = assess_stage("identity", items)
        self.assertIs(result.state, StageState.CONFLICT)
        self.assertIn(original.claim, result.conflicting_claims)
        self.assertEqual(result.reason_code, "EXPLICIT_CONFLICT_EVIDENCE")

    def test_missing_evidence_does_not_veto_a_confirmed_required_claim(self):
        items = items_for("identity")
        original = items[0]
        items.append(EvidenceItem(
            claim=original.claim,
            value=None,
            unit=original.unit,
            source_name="Source with no record in its coverage",
            source_url="https://registry.example/search",
            kind=EvidenceKind.OFFICIAL,
            state=EvidenceState.MISSING,
            retrieved_at=NOW,
        ))
        self.assertIs(assess_stage("identity", items).state, StageState.VERIFIED)

    def test_missing_required_claim_still_blocks_stage(self):
        items = items_for("identity")
        items[0] = EvidenceItem(
            claim=items[0].claim,
            value=None,
            unit=items[0].unit,
            source_name="Source with no record in its coverage",
            source_url="https://registry.example/search",
            kind=EvidenceKind.OFFICIAL,
            state=EvidenceState.MISSING,
            retrieved_at=NOW,
        )
        result = assess_stage("identity", items)
        self.assertIs(result.state, StageState.NEEDS_REVIEW)
        self.assertIn("isin", result.missing_claims)

    def test_sequential_report_cannot_be_complete_when_required_claim_conflicts(self):
        providers = {stage: (lambda _isin, stage=stage: items_for(stage)) for stage in STAGE_ORDER}
        identity = list(items_for("identity"))
        original = identity[0]
        identity.append(EvidenceItem(
            claim=original.claim,
            value="contradictory synthetic value",
            unit=original.unit,
            source_name="Conflicting official record",
            source_url="https://registry.example/record",
            kind=EvidenceKind.OFFICIAL,
            state=EvidenceState.CONFLICT,
            retrieved_at=NOW,
        ))
        providers["identity"] = lambda _isin: identity
        report = SequentialEvidenceVerifier().verify("RU000A123456", providers)
        self.assertFalse(report.complete)
        self.assertEqual(report.status, "RESEARCH_RECORD_CONFLICT")
        self.assertEqual(report.stages[0].conflicting_claims, ("isin",))

    def test_sequential_verification_stops_after_first_incomplete_stage(self):
        called = []

        def empty_identity(_isin):
            called.append("identity")
            return []

        def should_not_run(_isin):
            called.append("rating")
            return items_for("rating")

        report = SequentialEvidenceVerifier().verify(
            "RU000A123456",
            {"identity": empty_identity, "rating": should_not_run},
        )
        self.assertEqual(called, ["identity"])
        self.assertIs(report.stages[0].state, StageState.NEEDS_REVIEW)
        self.assertIs(report.stages[1].state, StageState.NOT_RUN)
        self.assertFalse(report.complete)

    def test_unverified_values_do_not_make_full_record_complete(self):
        called = []

        def provider(stage):
            def supply(_isin):
                called.append(stage)
                return items_for(stage)
            return supply

        providers = {stage: provider(stage) for stage in STAGE_ORDER}
        report = SequentialEvidenceVerifier().verify("RU000A123456", providers)
        self.assertTrue(report.complete)
        self.assertEqual(called, list(STAGE_ORDER))
        self.assertEqual(report.recommendation, "NONE")

    def test_insecure_or_malformed_source_urls_are_rejected(self):
        for url in ("http://issuer.example", "https://synthetic-user" + "@issuer.example/x",
                    "https://issuer.example:443/x"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    EvidenceItem(
                        claim="isin", value="RU000A123456", unit="",
                        source_name="Test", source_url=url,
                        kind=EvidenceKind.PRIMARY, state=EvidenceState.CONFIRMED,
                        retrieved_at=NOW,
                    )

    def test_rating_for_another_issue_cannot_verify_the_target_isin(self):
        items = items_for("rating")
        index = next(i for i, item in enumerate(items) if item.claim == "rating_subject_id")
        items[index] = replace(items[index], value="RU000A654321")
        result = assess_stage("rating", items, expected_isin="RU000A123456")
        self.assertIs(result.state, StageState.NEEDS_REVIEW)
        self.assertEqual(result.reason_code, "RATING_SUBJECT_MISMATCH_OR_UNKNOWN")

    def test_withdrawn_or_noncurrent_rating_is_not_current_evidence(self):
        items = items_for("rating")
        index = next(i for i, item in enumerate(items) if item.claim == "rating_status")
        items[index] = replace(items[index], value="WITHDRAWN")
        result = assess_stage("rating", items, expected_isin="RU000A123456")
        self.assertIs(result.state, StageState.NEEDS_REVIEW)
        self.assertEqual(result.reason_code, "RATING_NOT_CONFIRMED_ACTIVE")

    def test_historical_rating_action_must_be_identified_as_latest(self):
        items = items_for("rating")
        index = next(i for i, item in enumerate(items) if item.claim == "rating_action_is_latest")
        items[index] = replace(items[index], value=False)
        result = assess_stage("rating", items, expected_isin="RU000A123456")
        self.assertIs(result.state, StageState.NEEDS_REVIEW)
        self.assertEqual(result.reason_code, "LATEST_RATING_ACTION_NOT_CONFIRMED")

    def test_unknown_future_coupon_terms_do_not_complete_the_terms_stage(self):
        items = items_for("coupon_terms")
        index = next(i for i, item in enumerate(items) if item.claim == "future_coupon_terms_legally_defined")
        items[index] = replace(items[index], value="UNKNOWN")
        result = assess_stage("coupon_terms", items)
        self.assertIs(result.state, StageState.NEEDS_REVIEW)
        self.assertEqual(result.reason_code, "FUTURE_COUPON_TERMS_UNKNOWN")

    def test_superseded_document_cannot_verify_coupon_terms(self):
        items = items_for("coupon_terms")
        changed = []
        for item in items:
            provenance = replace(item.document_provenance, source_status="SUPERSEDED")
            changed.append(replace(item, document_provenance=provenance))
        result = assess_stage("coupon_terms", changed)
        self.assertIs(result.state, StageState.NEEDS_REVIEW)


if __name__ == "__main__":
    unittest.main()
