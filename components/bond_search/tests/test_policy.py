import unittest
from dataclasses import replace
from datetime import date

from msp_bonds_bond_search.policy import (
    CandidateFacts, PolicyConfigurationError, ScreenStatus,
    SelectionPolicy, RuleDisposition, rule_disposition,
)
from policy_fixtures import RULES, snapshot


def valid_facts(**overrides):
    data = dict(
        issuer_country="RU",
        instrument_type="CORPORATE_BOND",
        currency="RUB",
        rating_grade="BBB",
        rating_confirmed=True,
        rating_agency="Agency A",
        rating_scale="National",
        rating_subject_matches=True,
        rating_action_date=date(2026, 10, 1),
        floating_coupon=False,
        floating_formula_officially_confirmed=None,
        future_coupon_terms_legally_defined=True,
        maturity_date=date(2027, 11, 15),
        as_of=date(2026, 11, 15),
        existing_bbb_units_by_issuer=4,
        proposed_units=1,
    )
    data.update(overrides)
    return CandidateFacts(**data)


class PolicyEngineTests(unittest.TestCase):
    def test_approved_values_are_derived_and_no_unapproved_threshold_is_created(self):
        policy = SelectionPolicy.from_snapshot(snapshot())
        self.assertEqual(policy.minimum_rating, "BBB")
        self.assertEqual(policy.bbb_units_limit, 5)
        self.assertEqual(policy.preferred_maturity_years, 3)
        self.assertIsNone(policy.minimum_yield_percent)
        self.assertIsNone(policy.percentage_concentration_limit)
        self.assertIsNone(policy.nominal_constraint)
        self.assertIsNone(policy.nominal_constraint)

    def test_historical_rule_does_not_become_binding_from_status_alone(self):
        self.assertIs(rule_disposition("ИСТОРИЧЕСКАЯ МЕТОДИКА"), RuleDisposition.NON_BINDING)
        rows = [list(row) for row in RULES]
        rows[-1][5] = "УТВЕРЖДЕНО"
        policy = SelectionPolicy.from_snapshot(snapshot(rules=rows))
        self.assertIs(policy.disposition(30), RuleDisposition.NON_BINDING)

    def test_approved_bbb_unit_limit_applies(self):
        result = SelectionPolicy.from_snapshot(snapshot()).evaluate(valid_facts())
        self.assertIs(result.status, ScreenStatus.PASS_RESEARCH_ONLY)
        blocked = SelectionPolicy.from_snapshot(snapshot()).evaluate(
            valid_facts(existing_bbb_units_by_issuer=5, proposed_units=1),
        )
        self.assertIs(blocked.status, ScreenStatus.REJECTED_BY_POLICY)
        self.assertIn("approved_bbb_issuer_unit_limit_exceeded", blocked.reasons)

    def test_below_minimum_rating_rejected(self):
        result = SelectionPolicy.from_snapshot(snapshot()).evaluate(valid_facts(rating_grade="BBB-"))
        self.assertIs(result.status, ScreenStatus.REJECTED_BY_POLICY)
        self.assertIn("rating_below_approved_minimum", result.reasons)

    def test_a_minus_is_not_subject_to_five_unit_limit_but_requires_deeper_review(self):
        result = SelectionPolicy.from_snapshot(snapshot()).evaluate(
            valid_facts(rating_grade="A-", existing_bbb_units_by_issuer=None, proposed_units=None),
        )
        self.assertIs(result.status, ScreenStatus.PASS_RESEARCH_ONLY)
        self.assertIn("enhanced_credit_review_required_below_A", result.preference_flags)
        self.assertFalse(any("bbb_issuer_unit" in item for item in result.missing_evidence))

    def test_bbb_plus_interpretation_is_not_a_hard_rejection(self):
        result = SelectionPolicy.from_snapshot(snapshot()).evaluate(
            valid_facts(rating_grade="BBB+", existing_bbb_units_by_issuer=5, proposed_units=1),
        )
        self.assertIs(result.status, ScreenStatus.PASS_RESEARCH_ONLY)
        self.assertTrue(any("working interpretation" in item for item in result.preference_flags))

    def test_bbb_plus_missing_portfolio_count_requires_review(self):
        result = SelectionPolicy.from_snapshot(snapshot()).evaluate(
            valid_facts(rating_grade="BBB+", existing_bbb_units_by_issuer=None, proposed_units=None),
        )
        self.assertIs(result.status, ScreenStatus.NEEDS_EVIDENCE)

    def test_three_year_horizon_is_a_preference_not_a_rejection(self):
        result = SelectionPolicy.from_snapshot(snapshot()).evaluate(
            valid_facts(maturity_date=date(2031, 1, 1)),
        )
        self.assertIs(result.status, ScreenStatus.PASS_RESEARCH_ONLY)
        self.assertIn("maturity_exceeds_preferred_horizon_not_a_rejection", result.preference_flags)

    def test_float_formula_and_future_coupon_terms_are_required(self):
        policy = SelectionPolicy.from_snapshot(snapshot())
        missing_formula = policy.evaluate(valid_facts(
            floating_coupon=True,
            floating_formula_officially_confirmed=None,
        ))
        self.assertIs(missing_formula.status, ScreenStatus.NEEDS_EVIDENCE)
        undefined_terms = policy.evaluate(valid_facts(future_coupon_terms_legally_defined=False))
        self.assertIs(undefined_terms.status, ScreenStatus.REJECTED_BY_POLICY)

    def test_missing_rating_evidence_does_not_pass(self):
        result = SelectionPolicy.from_snapshot(snapshot()).evaluate(
            valid_facts(rating_confirmed=False, rating_action_date=None),
        )
        self.assertIs(result.status, ScreenStatus.NEEDS_EVIDENCE)

    def test_nominal_row_change_fails_closed(self):
        rows = [list(row) for row in RULES]
        rows[8][2] = "Обязателен номинал от 1 000 рублей."
        with self.assertRaises(PolicyConfigurationError):
            SelectionPolicy.from_snapshot(snapshot(rules=rows))

    def test_required_policy_status_change_fails_closed(self):
        rows = [list(r) for r in RULES]
        rows[9][5] = "ИСТОРИЧЕСКИЙ ПОДХОД"  # rule 61 status
        with self.assertRaises(PolicyConfigurationError):
            SelectionPolicy.from_snapshot(snapshot(rules=rows))


if __name__ == "__main__":
    unittest.main()
