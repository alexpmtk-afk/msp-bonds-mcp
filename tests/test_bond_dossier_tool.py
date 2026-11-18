"""No-network checks for the read-only dossier MCP entrypoint."""
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from msp_bonds_mcp.core.bond_dossier import CHECKS, build_plan, analyze_bond, valid_isin


def sample_snapshot():
    names = {name for _, name, _ in CHECKS}
    names.update("extra-registered-source-" + str(i) for i in range(26))
    return SimpleNamespace(
        rules=[SimpleNamespace(rule_id=n, status="ДЕЙСТВУЕТ") for n in range(1, 95)],
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

    def test_covers_all_94_rules_without_false_positives(self):
        plan = build_plan(sample_snapshot(), include_ledger=True)
        self.assertEqual(plan["methodology"]["rules"], 94)
        self.assertEqual(plan["methodology"]["mapped_checks"], 27)
        self.assertGreaterEqual(plan["methodology"]["registered_sources"], 26)
        self.assertEqual(len(plan["rule_ledger"]), 94)
        self.assertEqual(plan["recommendation"], "NONE")
        self.assertEqual(plan["gates"]["FULL_DOSSIER"], "NOT_READY")
        self.assertTrue(all(x["status"] == "NOT_COLLECTED" for x in plan["checks"]))

    def test_skipped_rule_rejected(self):
        broken = sample_snapshot()
        broken.rules.pop()
        broken.rules.append(SimpleNamespace(rule_id=95, status="ДЕЙСТВУЕТ"))
        with self.assertRaisesRegex(ValueError, "CANONICAL_RULES"):
            build_plan(broken)

    def test_disconnected_policy_returns_safe_status(self):
        with patch.dict(os.environ, {"MSP_BONDS_POLICY_SPREADSHEET_ID": "synthetic-id"}):
            result = analyze_bond("RU000A10FGE6")
        self.assertEqual(result["status"], "POLICY_UNAVAILABLE_OR_INVALID")


if __name__ == "__main__":
    unittest.main()
