import unittest
from datetime import date, datetime, timezone

from msp_bonds_bond_search.evidence import (
    EvidenceItem,
    EvidenceKind,
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
    for claim in REQUIRED_CLAIMS[stage]:
        kind = EvidenceKind.CALCULATION if stage == "yield" and claim in {"full_entry_cost", "xirr"} else EvidenceKind.PRIMARY
        result.append(EvidenceItem(
            claim=claim,
            value=f"synthetic-{claim}",
            unit="synthetic",
            source_name="Synthetic official record",
            source_url="https://issuer.example/document",
            kind=kind,
            state=EvidenceState.CONFIRMED,
            retrieved_at=NOW,
            document_date=date(2026, 11, 14),
            method="offline test fixture",
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


if __name__ == "__main__":
    unittest.main()
