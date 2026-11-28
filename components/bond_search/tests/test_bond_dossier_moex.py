from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from msp_bonds_mcp.core import bond_dossier

ISIN = "RU000A10FGE6"
STAMP = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


class BondDossierMoexIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, {"MSP_BONDS_POLICY_SPREADSHEET_ID": "offline-test"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.workspace_patch = patch(
            "msp_bonds_bond_search.google_source.load_from_existing_workspace",
            return_value=object(),
        )
        self.workspace_patch.start()
        self.addCleanup(self.workspace_patch.stop)
        self.plan_patch = patch.object(
            bond_dossier, "build_plan",
            return_value={"status": "RESEARCH_NOT_COMPLETED", "checks": []},
        )
        self.plan_patch.start()
        self.addCleanup(self.plan_patch.stop)

    def _match(self, matched=True):
        return SimpleNamespace(
            state="IDENTIFIER_CONFIRMED_ON_MOEX" if matched else "INCONCLUSIVE",
            matched=matched,
            security_id="IEKHOLD1P5" if matched else "",
            short_name="IEK",
            source_url="https://iss.moex.com/iss/securities.json?q=" + ISIN,
            fetched_at=STAMP,
            scan_complete=False,
        )

    def test_default_call_does_not_request_moex(self):
        with patch("msp_bonds_bond_search.discovery.lookup_moex_isin") as lookup:
            with patch("msp_bonds_bond_search.moex_bond_facts.fetch_moex_bond_facts") as facts:
                result = bond_dossier.analyze_bond(ISIN)
        lookup.assert_not_called()
        facts.assert_not_called()
        self.assertEqual(result["moex_lookup"]["status"], "NOT_REQUESTED")
        self.assertEqual(result["moex_facts"]["status"], "NOT_REQUESTED")

    def test_explicit_moex_call_fetches_facts_after_exact_match(self):
        with patch("msp_bonds_bond_search.discovery.lookup_moex_isin", return_value=self._match()):
            with patch(
                "msp_bonds_bond_search.moex_bond_facts.fetch_moex_bond_facts",
                return_value={"status": "DATA_RECEIVED", "fields": {"isin": {"value": ISIN}}},
            ) as facts:
                result = bond_dossier.analyze_bond(ISIN, check_moex=True)
        facts.assert_called_once()
        self.assertEqual(result["moex_facts"]["status"], "DATA_RECEIVED")
        self.assertEqual(result["moex_facts"]["fields"]["isin"]["value"], ISIN)

    def test_inconclusive_identifier_never_fetches_security_facts(self):
        with patch(
            "msp_bonds_bond_search.discovery.lookup_moex_isin",
            return_value=self._match(matched=False),
        ):
            with patch("msp_bonds_bond_search.moex_bond_facts.fetch_moex_bond_facts") as facts:
                result = bond_dossier.analyze_bond(ISIN, check_moex=True)
        facts.assert_not_called()
        self.assertEqual(result["moex_facts"]["status"], "NOT_FETCHED")
        self.assertEqual(result["moex_facts"]["reason_code"], "EXACT_MOEX_IDENTIFIER_NOT_CONFIRMED")


if __name__ == "__main__":
    unittest.main()