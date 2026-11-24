"""No-network checks for the read-only dossier MCP entrypoint."""
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from msp_bonds_mcp.core.bond_dossier import CHECKS, build_plan, analyze_bond, valid_isin


def sample_snapshot(rule_count=97):
    names = {name for _, name, _ in CHECKS}
    names.update("extra-registered-source-" + str(i) for i in range(26 - len(names)))
    return SimpleNamespace(
        rules=[SimpleNamespace(rule_id=n, status="ДЕЙСТВУЕТ") for n in range(1, rule_count + 1)],
        resources=[SimpleNamespace(name=n) for n in sorted(names)],
        fingerprint="synthetic-policy",
        fetched_at=datetime.now(timezone.utc),
        assert_fresh=lambda: None,
    )


class DossierToolTests(unittest.TestCase):
    def test_valid_isin_with_check_digit(self):
        self.assertTrue(valid_isin("RU000A10FGE6"))
        self.assertFalse(valid_isin("RU000A10FGE7"))
        self.assertFalse(valid_isin("RU000A10FGE6\n"))

    def test_no_local_policy_config_causes_no_external_access(self):
        with patch.dict(os.environ, {"MSP_BONDS_POLICY_SPREADSHEET_ID": ""}):
            result = analyze_bond("RU000A10FGE6", check_moex=True)
        self.assertEqual(result["status"], "NOT_CONFIGURED")

    def test_covers_all_97_rules_without_false_positives(self):
        plan = build_plan(sample_snapshot(), include_ledger=True)
        self.assertEqual(plan["methodology"]["rules"], 97)
        self.assertEqual(plan["methodology"]["mapped_checks"], 27)
        self.assertGreaterEqual(plan["methodology"]["registered_sources"], 26)
        self.assertEqual(len(plan["rule_ledger"]), 97)
        self.assertEqual(plan["recommendation"], "NONE")
        self.assertEqual(plan["gates"]["FULL_DOSSIER"], "NOT_READY")
        self.assertTrue(all(x["status"] == "NOT_COLLECTED" for x in plan["checks"]))

    def test_workflow_rule_mapping(self):
        plan = build_plan(sample_snapshot(), include_ledger=True)
        ledger = {row["id"]: row for row in plan["rule_ledger"]}
        for n in (95, 96):
            self.assertEqual(ledger[n]["scope"], "CONTEXT_OR_DEVELOPMENT")
            self.assertEqual(ledger[n]["planned_checks"], [])
        self.assertEqual(ledger[97]["scope"], "MAPPED_NOT_EXECUTED")
        self.assertEqual(ledger[97]["planned_checks"], [code for code, _, _ in CHECKS])
        self.assertEqual(plan["methodology"]["executed_credit_checks"], 0)
        self.assertEqual(plan["source_status"], "NOT_QUERIED")
        self.assertEqual(len(plan["source_ledger"]), 26)

    def test_stale_policy_rejected(self):
        with self.assertRaisesRegex(ValueError, "CANONICAL_RULES"):
            build_plan(sample_snapshot(94))

    def test_unmapped_future_rule_rejected(self):
        with self.assertRaisesRegex(ValueError, "RULES_WITHOUT_MAPPING_OR_CONTEXT"):
            build_plan(sample_snapshot(98))

    def test_skipped_rule_rejected(self):
        broken = sample_snapshot()
        broken.rules.pop()
        broken.rules.append(SimpleNamespace(rule_id=98, status="ДЕЙСТВУЕТ"))
        with self.assertRaisesRegex(ValueError, "CANONICAL_RULES"):
            build_plan(broken)

    def test_disconnected_policy_returns_safe_status(self):
        with patch.dict(os.environ, {"MSP_BONDS_POLICY_SPREADSHEET_ID": "synthetic-id"}):
            result = analyze_bond("RU000A10FGE6")
        self.assertEqual(result["status"], "POLICY_UNAVAILABLE_OR_INVALID")


if __name__ == "__main__":
    unittest.main()
