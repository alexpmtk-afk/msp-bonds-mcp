"""Offline tests only: no network, no brokerage positions, no credentials."""
import unittest
from datetime import datetime, timedelta, timezone

from msp_bonds_bond_search.catalog import PolicySourceSnapshot, SnapshotError
from msp_bonds_bond_search.discovery import parse_smartlab, parse_moex, SourceUnavailable
from msp_bonds_bond_search.pipeline import DiscoveryService
from msp_bonds_bond_search.routes import plan_evidence_route

NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
RULES = [
    ["№", "Этап", "Правило / проверка", "Что фиксировать и как считать", "Где проверять", "Статус", "Примечание / основание"],
    ["1", "Проверка фактов", "Не использовать неизвестные значения", "Отметить пробел", "Тест", "ДЕЙСТВУЕТ", "Только синтетические данные"],
]
SOURCES = [
    ["Приоритет", "Ресурс", "Ссылка", "Где и что смотреть", "Как использовать / сверять", "Статус", "Этапы логики"],
    ["Начальный", "Smart-Lab", "https://smart-lab.ru/", "Первичный поиск", "Дальнейшая проверка", "ПЛАН", "1"],
    ["Альтернатива", "MOEX ISS", "https://www.moex.com/a8531", "Инструменты", "Биржевой API", "ПЛАН", "1"],
]


def snapshot():
    return PolicySourceSnapshot.from_sheet_rows(RULES, SOURCES, fetched_at=NOW)


class CatalogTests(unittest.TestCase):
    def test_load_snapshot(self):
        value = snapshot()
        self.assertEqual(value.rules[0].rule_id, 1)
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


class DiscoveryTests(unittest.TestCase):
    def test_smartlab_parser_extracts_isin_not_quote(self):
        html = '<table><tr><th>№</th><th>Имя</th></tr><tr><td>1</td><td><a href="/q/bonds/RU000A123456/">Тестовый выпуск</a></td><td>99.5</td></tr></table>'
        result = parse_smartlab(html, fetched_at=NOW)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].isin, "RU000A123456")
        self.assertEqual(result[0].status, "DISCOVERED_UNVERIFIED")

    def test_moex_dynamic_columns(self):
        raw = b'{"securities":{"columns":["SHORTNAME","ISIN","SECID"],"data":[["Sample","RU000A123456","RU000A123456"]]}}'
        result = parse_moex(raw, fetched_at=NOW)
        self.assertEqual(result[0].name, "Sample")
        self.assertEqual(result[0].isin, "RU000A123456")

    def test_authorization_required(self):
        service = DiscoveryService(snapshot=snapshot(), now=lambda: NOW)
        with self.assertRaises(PermissionError):
            service.discover()

    def test_smartlab_first_and_no_recommendation(self):
        def get(url):
            self.assertIn("smart-lab.ru", url)
            return b'<table><tr><td>1</td><td><a href="/q/bonds/RU000A123456/">Example</a></td></tr></table>'
        result = DiscoveryService(snapshot=snapshot(), fetch=get, now=lambda: NOW).discover(task_authorized=True)
        self.assertEqual(result.used_source, "Smart-Lab")
        self.assertEqual(result.status, "RESEARCH_ONLY_NOT_VERIFIED")
        self.assertGreater(len(result.unimplemented_checks), 0)

    def test_fallback_to_moex(self):
        def get(url):
            if "smart-lab.ru" in url:
                raise SourceUnavailable("offline")
            return b'{"securities":{"columns":["SECID","SHORTNAME"],"data":[["RU000A123456","Example"]]}}'
        result = DiscoveryService(snapshot=snapshot(), fetch=get, now=lambda: NOW).discover(task_authorized=True)
        self.assertEqual(result.used_source, "MOEX ISS")
        self.assertEqual(len(result.source_errors), 1)

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
        with_missing = plan_evidence_route(snapshot(), "rating")
        self.assertEqual(with_missing.field, "rating")
        self.assertTrue(with_missing.missing_from_registry)
        self.assertFalse(with_missing.providers)

    def test_discovery_route_uses_catalog_order(self):
        result = plan_evidence_route(snapshot(), "discovery")
        self.assertEqual([s.name for s in result.providers], ["Smart-Lab", "MOEX ISS"])
        self.assertIn("Bonds-Lab", result.missing_from_registry)

    def test_missing_evidence_sources_block(self):
        with self.assertRaises(SnapshotError):
            plan_evidence_route(snapshot(), "rating")


if __name__ == "__main__":
    unittest.main()
