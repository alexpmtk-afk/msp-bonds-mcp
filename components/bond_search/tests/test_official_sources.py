from datetime import datetime, timezone
import unittest

from msp_bonds_bond_search.evidence import EvidenceKind, EvidenceState
from msp_bonds_bond_search.official_sources import (
    load_moex_identifier,
    provider_capabilities,
)


TEST_ISIN = "RU0000000000"


class OfficialSourceTests(unittest.TestCase):
    def test_exact_identifier_produces_only_identifier_evidence(self):
        payload = (
            b'{"securities":{"columns":["ISIN","SECID","SHORTNAME"],'
            b'"data":[["RU0000000000","SYNTHETIC01","Synthetic sample"]]}}'
        )
        result = load_moex_identifier(
            TEST_ISIN,
            fetch=lambda url: payload,
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(result.status, "IDENTIFIER_CONFIRMED_ONLY")
        self.assertEqual(len(result.evidence), 1)
        item = result.evidence[0]
        self.assertEqual(item.claim, "isin")
        self.assertEqual(item.value, TEST_ISIN)
        self.assertEqual(item.kind, EvidenceKind.OFFICIAL)
        self.assertEqual(item.state, EvidenceState.CONFIRMED)
        self.assertEqual(result.short_name, "Synthetic sample")

    def test_absent_identifier_returns_no_evidence(self):
        payload = b'{"securities":{"columns":["ISIN","SECID"],"data":[]}}'
        result = load_moex_identifier(TEST_ISIN, fetch=lambda url: payload)
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertEqual(result.evidence, ())

    def test_other_official_sources_are_explicitly_not_automated(self):
        states = {item.field: item.status for item in provider_capabilities()}
        self.assertEqual(states["identity"], "IMPLEMENTED_IDENTIFIER_ONLY")
        self.assertEqual(states["rating"], "MANUAL_SOURCE_ONLY")
        self.assertEqual(states["coupon_terms"], "MANUAL_SOURCE_ONLY")
        self.assertEqual(states["amortization"], "MANUAL_SOURCE_ONLY")
        self.assertEqual(states["default_events"], "MANUAL_SOURCE_ONLY")
        self.assertEqual(states["financial_condition"], "REST_API_ACCESS_REQUIRED")


if __name__ == "__main__":
    unittest.main()
