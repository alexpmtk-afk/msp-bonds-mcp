"""Offline tests only: no network, no brokerage positions, no credentials."""
import unittest
from datetime import datetime, timedelta, timezone

from msp_bonds_bond_search.catalog import PolicySourceSnapshot, SnapshotError
from msp_bonds_bond_search.discovery import (
    SourceUnavailable, discover_smartlab, parse_moex, parse_smartlab,
)
from msp_bonds_bond_search.pipeline import DiscoveryService
from msp_bonds_bond_search.routes import plan_evidence_route
from policy_fixtures import RULES, SOURCES, snapshot

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


class CatalogTests(unittest.TestCase):
    def test_load_snapshot(self):
        value = snapshot()
        self.assertGreaterEqual(len(value.rules), 18)
        self.assertEqual(len(value.resources), 2)
        self.assertEqual(len(value.fingerprint), 64)

    def test_stale_snapshot_rejected(self):
        with self.assertRaises(SnapshotError):
            snapshot().assert_fresh(now=NOW + timedelta(hours=2))

    def test_duplicate_rule_rejected(self):
        with self.assertRaises(SnapshotError):
            PolicySourceSnapshot.from_sheet_rows(RULES + [RULES[1]], SOURCES, fetched_at=NOW)

    def test_missing_resource_rejected(self):
        with self.assertRaises(SnapshotError):
            snapshot().resource("unknown")

    def test_ragged_google_rows_are_padded_but_wrong_headers_rejected(self):
        rules = [list(row) for row in RULES]
        rules[1] = rules[1][:6]
        self.assertEqual(len(PolicySourceSnapshot.from_sheet_rows(rules, SOURCES, fetched_at=NOW).rules), len(RULES) - 1)
        rules[0][0] = "Wrong"
        with self.assertRaises(SnapshotError):
            PolicySourceSnapshot.from_sheet_rows(rules, SOURCES, fetched_at=NOW)


class DiscoveryTests(unittest.TestCase):
    def test_smartlab_parser_extracts_isin_not_quote(self):
        html = '<table><tr><th>№</th><th>Имя</th></tr><tr><td>1</td><td><a href="/q/bonds/RU000A123456/">Тестовый выпуск</a></td><td>99.5</td></tr></table>'
        result = parse_smartlab(html, fetched_at=NOW)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].isin, "RU000A123456")
        self.assertEqual(result[0].status, "DISCOVERED_UNVERIFIED")

    def test_smartlab_requires_robots_allow_before_page_fetch(self):
        calls = []
        def get(url):
            calls.append(url)
            return b"User-agent: *\nDisallow: /q/bonds/\n"
        with self.assertRaises(SourceUnavailable):
            discover_smartlab(fetch=get, now=NOW)
        self.assertEqual(calls, ["https://smart-lab.ru/robots.txt"])

    def test_moex_dynamic_columns(self):
        raw = b'{"securities":{"columns":["SHORTNAME","ISIN","SECID"],"data":[["Sample","RU000A123456","RU000A123456"]]}}'
        result = parse_moex(raw, fetched_at=NOW)
        self.assertEqual(result[0].name, "Sample")
        self.assertEqual(result[0].isin, "RU000A123456")

    def test_authorization_required(self):
        service = DiscoveryService(snapshot=snapshot(), now=lambda: NOW)
        with self.assertRaises(PermissionError):
            service.discover()

    def test_smartlab_first_and_moex_independent_confirmation_stays_unverified(self):
        def get(url):
            if url.endswith("/robots.txt"):
                return b"User-agent: *\nAllow: /q/bonds/\n"
            if url == "https://smart-lab.ru/q/bonds/":
                return b'<table><tr><td>1</td><td><a href="/q/bonds/RU000A123456/">Example</a></td></tr></table>'
            if url.startswith("https://iss.moex.com/iss/securities.json?q="):
                return b'{"securities":{"columns":["ISIN","SECID","SHORTNAME"],"data":[["RU000A123456","SEC123","Example"]]}}'
            raise AssertionError("unexpected URL")
        result = DiscoveryService(snapshot=snapshot(), fetch=get, now=lambda: NOW, moex_verification_limit=1).discover(task_authorized=True)
        self.assertEqual(result.used_source, "Smart-Lab")
        self.assertEqual(result.candidates[0].independent_sources, ("MOEX ISS",))
        self.assertEqual(result.candidates[0].status, "DISCOVERED_UNVERIFIED")
        self.assertEqual(result.status, "RESEARCH_ONLY_NOT_VERIFIED")

    def test_fallback_to_moex(self):
        def get(url):
            if url.endswith("/robots.txt"):
                raise SourceUnavailable("robots unavailable")
            if "/markets/bonds/securities.json" in url:
                return b'{"securities":{"columns":["SECID","SHORTNAME","ISIN"],"data":[["RU000A123456","Example","RU000A123456"]]}}'
            raise AssertionError("unexpected URL")
        result = DiscoveryService(snapshot=snapshot(), fetch=get, now=lambda: NOW).discover(task_authorized=True)
        self.assertEqual(result.used_source, "MOEX ISS")
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].status, "DISCOVERED_UNVERIFIED")

    def test_both_sources_unavailable(self):
        def get(url):
            raise SourceUnavailable("offline")
        result = DiscoveryService(snapshot=snapshot(), fetch=get, now=lambda: NOW).discover(task_authorized=True)
        self.assertEqual(result.status, "UNAVAILABLE_NO_RESULTS")
        self.assertFalse(result.candidates)

    def test_stale_policy_blocks_network(self):
        def get(url):
            self.fail("network should not be accessed for stale policy")
        service = DiscoveryService(snapshot=snapshot(), fetch=get, now=lambda: NOW + timedelta(hours=2))
        with self.assertRaises(SnapshotError):
            service.discover(task_authorized=True)


class RouteTests(unittest.TestCase):
    def test_rating_route_lists_alternatives(self):
        rating = ["Основной", "Банк России — рейтинговый репозитарий",
                  "https://ratings.cbr.ru/", "Рейтинг", "Сверка", "ПЛАН", "1"]
        configured = PolicySourceSnapshot.from_sheet_rows(
            RULES, SOURCES + [rating], fetched_at=NOW,
        )
        result = plan_evidence_route(configured, "rating")
        self.assertEqual(result.providers[0].name, "Банк России — рейтинговый репозитарий")
        self.assertIn("АКРА", result.missing_from_registry)
        self.assertEqual(result.routed_only[0], "Банк России — рейтинговый репозитарий")
        self.assertEqual(result.status, "ROUTE_ONLY_NOT_VERIFIED")

    def test_discovery_route_separates_live_adapter_from_route_only_source(self):
        result = plan_evidence_route(snapshot(), "discovery")
        self.assertEqual([s.name for s in result.providers], ["Smart-Lab", "MOEX ISS"])
        self.assertEqual(result.available_adapters, ("Smart-Lab", "MOEX ISS"))
        self.assertIn("Bonds-Lab", result.missing_from_registry)


if __name__ == "__main__":
    unittest.main()
