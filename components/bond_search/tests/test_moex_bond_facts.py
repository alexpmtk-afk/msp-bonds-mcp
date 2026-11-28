from __future__ import annotations

from datetime import datetime, timezone
import json
import unittest

from msp_bonds_bond_search.discovery import SourceUnavailable
from msp_bonds_bond_search.moex_bond_facts import fetch_moex_bond_facts, parse_moex_bond_facts

ISIN = "RU000A10FGE6"
SECID = "IEKHOLD1P5"
STAMP = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
IDENTIFIER_URL = "https://iss.moex.com/iss/securities.json?q=RU000A10FGE6"
SPEC_URL = f"https://iss.moex.com/iss/securities/{SECID}.json?iss.meta=off&iss.only=description,boards"
MARKET_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/bonds/securities/"
    f"{SECID}.json?iss.meta=off&iss.only=securities,marketdata"
)


def fixtures(*, second_board_price=90.2, second_face="1000"):
    specification = {
        "description": {
            "columns": ["name", "title", "value"],
            "data": [
                ["ISIN", "ISIN", ISIN],
                ["SECID", "Код ценной бумаги", SECID],
                ["ISSUER", "Эмитент", "ПАО ИЭК ХОЛДИНГ"],
                ["ISSUEDATE", "Дата выпуска", "2026-06-19"],
                ["SHORTNAME", "Краткое имя", "ИЭКХолд1Р5"],
                ["SECNAME", "Полное имя", "ИЭК ХОЛДИНГ 001Р-05"],
                ["FACEVALUE", "Номинал", "1000"],
                ["MATDATE", "Погашение", "2028-12-05"],
                ["COUPONPERCENT", "Ставка", "15.75"],
                ["COUPONVALUE", "Купон", "12.95"],
                ["COUPONPERIOD", "Период", "30"],
            ],
        },
        "boards": {
            "columns": ["boardid", "title", "is_primary", "is_traded"],
            "data": [["TQOB", "Т+: Облигации", 1, 1], ["TQOD", "Облигации Д", 0, 1]],
        },
    }
    market = {
        "securities": {
            "columns": [
                "SECID", "BOARDID", "ISIN", "SHORTNAME", "SECNAME", "FACEVALUE", "FACEUNIT",
                "ISSUEDATE", "MATDATE", "COUPONPERCENT", "COUPONVALUE",
                "COUPONPERIOD", "COUPONFREQUENCY", "NEXTCOUPON",
                "STATUS", "TRADINGSTATUS",
            ],
            "data": [
                [SECID, "TQOB", ISIN, "ИЭКХолд1Р5", "ИЭК ХОЛДИНГ 001Р-05", "1000", "RUB",
                 "2026-06-19", "2028-12-05", "15.75", "12.95", "30", "12",
                 "2026-12-16", "A", "N"],
                [SECID, "TQOD", ISIN, "ИЭКХолд1Р5", "ИЭК ХОЛДИНГ 001Р-05", second_face, "RUB",
                 "2026-06-19", "2028-12-05", "15.75", "12.95", "30", "12",
                 "2026-12-16", "A", "N"],
            ],
        },
        "marketdata": {
            "columns": ["SECID", "BOARDID", "LAST", "ACCRUEDINT", "TRADEDATE", "SYSTIME"],
            "data": [
                [SECID, "TQOB", "90.15", "3.45", "2026-11-20", "18:45:00"],
                [SECID, "TQOD", second_board_price, "3.46", "2026-11-20", "18:44:00"],
            ],
        },
    }
    return specification, market


def parse(specification, market, *, errors=()):
    return parse_moex_bond_facts(
        ISIN, SECID, specification=specification, market=market,
        identifier_source_url=IDENTIFIER_URL, identifier_fetched_at=STAMP,
        specification_source_url=SPEC_URL, specification_fetched_at=STAMP,
        market_source_url=MARKET_URL, market_fetched_at=STAMP,
        source_errors=tuple(errors),
    )


class MoexBondFactsTests(unittest.TestCase):
    def test_parses_source_attributed_instrument_fields(self):
        specification, market = fixtures()
        result = parse(specification, market)
        self.assertEqual(result["status"], "DATA_RECEIVED")
        self.assertEqual(result["identity_state"], "EXACT_LOOKUP_CONFIRMED")
        self.assertEqual(result["fields"]["face_value"]["value"], 1000)
        self.assertEqual(result["fields"]["face_currency"]["value"], "RUB")
        self.assertEqual(result["fields"]["maturity_date"]["value"], "2028-12-05")
        self.assertEqual(result["fields"]["coupon_amount"]["value"], 12.95)
        self.assertEqual(result["fields"]["coupon_rate_percent"]["value"], 15.75)
        self.assertEqual(result["fields"]["short_name"]["value"], "ИЭКХолд1Р5")
        self.assertEqual(result["fields"]["short_name"]["source_fields"], ["SHORTNAME"])
        self.assertEqual(result["fields"]["full_name"]["value"], "ИЭК ХОЛДИНГ 001Р-05")
        self.assertEqual(result["fields"]["full_name"]["source_fields"], ["SECNAME"])
        self.assertEqual(result["fields"]["security_name"]["value"], "ИЭК ХОЛДИНГ 001Р-05")
        self.assertIn(SPEC_URL, result["fields"]["issuer_name"]["source_urls"])
        self.assertEqual(result["fields"]["issuer_name"]["state"], "CONFIRMED_BY_MOEX")

    def test_short_and_full_name_fields_do_not_conflict(self):
        specification, market = fixtures()
        result = parse(specification, market)
        self.assertEqual(result["status"], "DATA_RECEIVED")
        self.assertEqual(result["fields"]["short_name"]["value"], "ИЭКХолд1Р5")
        self.assertEqual(result["fields"]["full_name"]["value"], "ИЭК ХОЛДИНГ 001Р-05")
        self.assertEqual(result["fields"]["short_name"]["state"], "CONFIRMED_BY_MOEX")
        self.assertEqual(result["fields"]["full_name"]["state"], "CONFIRMED_BY_MOEX")

    def test_conflict_between_same_semantic_name_columns_remains_visible(self):
        specification, market = fixtures()
        secname_index = market["securities"]["columns"].index("SECNAME")
        market["securities"]["data"][1][secname_index] = "ДРУГОЙ ВЫПУСК"
        result = parse(specification, market)
        self.assertEqual(result["fields"]["full_name"]["state"], "CONFLICT")
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(result["fields"]["full_name"]["candidates"][1]["source_field"], "SECNAME")

    def test_duplicate_description_fields_are_not_silently_overwritten(self):
        specification, market = fixtures()
        specification["description"]["data"].append(
            ["SECNAME", "Полное имя", "ДРУГОЙ ВЫПУСК"]
        )
        result = parse(specification, market)
        self.assertEqual(result["fields"]["full_name"]["state"], "CONFLICT")
        self.assertEqual(result["status"], "CONFLICT")
        self.assertTrue(any(
            item["source_field"] == "SECNAME"
            and item["value"] == "ДРУГОЙ ВЫПУСК"
            for item in result["fields"]["full_name"]["candidates"]
        ))

    def test_equivalent_name_spacing_and_case_are_not_a_conflict(self):
        specification, market = fixtures()
        secname_index = market["securities"]["columns"].index("SECNAME")
        market["securities"]["data"][1][secname_index] = "  иэк   холдинг 001р-05 "
        result = parse(specification, market)
        self.assertEqual(result["fields"]["full_name"]["state"], "CONFIRMED_BY_MOEX")
        self.assertEqual(result["status"], "DATA_RECEIVED")

    def test_different_values_in_synonymous_columns_remain_a_conflict(self):
        specification, market = fixtures()
        market["securities"]["columns"].append("FACE_VALUE")
        for row in market["securities"]["data"]:
            row.append("950")
        result = parse(specification, market)
        self.assertEqual(result["fields"]["face_value"]["state"], "CONFLICT")
        self.assertIn("FACEVALUE", result["fields"]["face_value"]["source_fields"])
        self.assertIn("FACE_VALUE", result["fields"]["face_value"]["source_fields"])
    def test_marks_absent_fields_instead_of_inventing_values(self):
        result = parse({}, {"securities": {"columns": ["SECID"], "data": [[SECID]]}})
        self.assertEqual(result["fields"]["maturity_date"]["state"], "NOT_AVAILABLE")
        self.assertIsNone(result["fields"]["maturity_date"]["value"])
        self.assertEqual(result["fields"]["coupon_rate_percent"]["state"], "NOT_AVAILABLE")

    def test_rejects_response_with_different_isin(self):
        specification, market = fixtures()
        market["securities"]["data"][0][2] = "RU000A10ZZZ0"
        result = parse(specification, market)
        self.assertEqual(result["status"], "IDENTITY_CONFLICT")
        self.assertEqual(result["identity_state"], "CONFLICT")
        self.assertEqual(result["fields"], {})

    def test_rejects_response_with_different_security_id(self):
        specification, market = fixtures()
        market["securities"]["data"][0][0] = "OTHERSEC"
        result = parse(specification, market)
        self.assertEqual(result["status"], "IDENTITY_CONFLICT")

    def test_flags_conflicting_face_values_across_boards(self):
        specification, market = fixtures(second_face="950")
        result = parse(specification, market)
        self.assertEqual(result["fields"]["face_value"]["state"], "CONFLICT")
        self.assertEqual(result["status"], "CONFLICT")
        self.assertIsNone(result["fields"]["face_value"]["value"])

    def test_keeps_market_values_separate_by_board(self):
        specification, market = fixtures(second_board_price=90.2)
        result = parse(specification, market)
        observations = {row["board_id"]: row for row in result["market_data_by_board"]}
        self.assertEqual(observations["TQOB"]["last_trade_price"]["value"], 90.15)
        self.assertEqual(observations["TQOB"]["last_trade_price"]["source_fields"], ["LAST"])
        self.assertEqual(observations["TQOB"]["accrued_interest"]["source_fields"], ["ACCRUEDINT"])
        self.assertEqual(observations["TQOD"]["last_trade_price"]["value"], 90.2)
        self.assertIn("not a live bid, ask", observations["TQOB"]["last_trade_price"]["note"])
        self.assertNotIn("selected", observations["TQOB"])
        boards = {row["board_id"]: row for row in result["trading_boards"]}
        self.assertEqual(boards["TQOB"]["is_primary"], 1)
        self.assertEqual(boards["TQOB"]["is_traded"], 1)
        self.assertEqual(boards["TQOB"]["security_status"], "A")
        self.assertEqual(boards["TQOB"]["trading_status"], "N")
        self.assertIn("TRADINGSTATUS", boards["TQOB"]["source_fields"])

    def test_preserves_coupon_period_and_next_coupon_without_interpolation(self):
        specification, market = fixtures()
        result = parse(specification, market)
        self.assertEqual(result["fields"]["coupon_period_days"]["value"], 30)
        self.assertEqual(result["fields"]["coupon_frequency_per_year"]["value"], 12)
        self.assertEqual(result["fields"]["next_coupon_date"]["value"], "2026-12-16")

    def test_malformed_numeric_value_is_not_confirmed(self):
        specification, market = fixtures()
        face_index = market["securities"]["columns"].index("FACEVALUE")
        market["securities"]["data"][0][face_index] = "unknown"
        market["securities"]["data"][1][face_index] = "unknown"
        result = parse(specification, market)
        self.assertEqual(result["fields"]["face_value"]["state"], "CONFLICT")
        self.assertEqual(result["status"], "CONFLICT")
        self.assertIsNone(result["fields"]["face_value"]["value"])

    def test_partial_failure_keeps_available_fields_and_safe_code(self):
        specification, market = fixtures()
        result = parse(specification, market, errors=({"endpoint": "security_specification", "code": "HTTP_503"},))
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["source_errors"][0]["code"], "HTTP_503")
        self.assertEqual(result["fields"]["face_value"]["value"], 1000)

    def test_successful_empty_marketdata_block_is_not_reported_as_request_failure(self):
        specification, market = fixtures()
        market["marketdata"]["data"] = []
        result = parse(specification, market)
        self.assertEqual(result["source_errors"], [])
        self.assertEqual(result["market_data_diagnostic"]["status"], "NO_ROWS_RETURNED")
        self.assertEqual(result["market_data_diagnostic"]["rows_received"], 0)
        self.assertIn("само по себе это не доказывает", result["market_data_diagnostic"]["interpretation"])
        self.assertIn("does not establish", " ".join(result["limitations"]))

    def test_unavailable_field_has_response_scoped_reason(self):
        specification, market = fixtures()
        specification["description"]["data"] = [
            row for row in specification["description"]["data"] if row[0] != "SECNAME"
        ]
        secname_index = market["securities"]["columns"].index("SECNAME")
        for row in market["securities"]["data"]:
            row[secname_index] = None
        result = parse(specification, market)
        fact = result["fields"]["full_name"]
        self.assertEqual(fact["state"], "NOT_AVAILABLE")
        self.assertEqual(fact["availability"]["reason_code"], "NO_VALUE_RETURNED")
        self.assertIn("не подтверждает", fact["availability"]["message"])

    def test_missing_expected_marketdata_block_is_a_parse_error(self):
        specification, market = fixtures()
        del market["marketdata"]

        def fetch(url):
            return json.dumps(specification if url == SPEC_URL else market).encode()

        result = fetch_moex_bond_facts(
            ISIN, SECID, identifier_source_url=IDENTIFIER_URL,
            identifier_fetched_at=STAMP, fetch=fetch, now=STAMP,
        )
        market_error = next(item for item in result["source_errors"] if item["endpoint"] == "market_data")
        self.assertEqual(market_error["code"], "MISSING_RESPONSE_BLOCK")
        self.assertEqual(market_error["phase"], "parse")
        self.assertEqual(result["sources"]["market_data"]["status"], "UNAVAILABLE")
        self.assertEqual(result["market_data_diagnostic"]["status"], "SOURCE_UNAVAILABLE")

    def test_endpoint_error_exposes_safe_phase_and_availability_reason(self):
        _, market = fixtures()

        def fetch(url):
            if url == SPEC_URL:
                raise SourceUnavailable(
                    "private transport detail", code="HTTP_503", phase="http",
                    http_status=503, attempts=2,
                )
            return json.dumps(market).encode()

        result = fetch_moex_bond_facts(
            ISIN, SECID, identifier_source_url=IDENTIFIER_URL,
            identifier_fetched_at=STAMP, fetch=fetch, now=STAMP,
        )
        error = next(item for item in result["source_errors"] if item["endpoint"] == "security_specification")
        self.assertEqual(error["code"], "HTTP_503")
        self.assertEqual(error["phase"], "http")
        self.assertEqual(error["http_status"], "503")
        self.assertEqual(error["attempts"], "2")
        issuer = result["fields"]["issuer_name"]
        self.assertEqual(issuer["state"], "NOT_AVAILABLE")
        self.assertEqual(issuer["availability"]["reason_code"], "SOURCE_RESPONSE_INCOMPLETE")
        self.assertNotIn("private transport detail", json.dumps(result))

    def test_bad_table_row_width_is_not_silently_truncated(self):
        specification, market = fixtures()
        market["marketdata"]["data"][0].pop()

        def fetch(url):
            return json.dumps(specification if url == SPEC_URL else market).encode()

        result = fetch_moex_bond_facts(
            ISIN, SECID, identifier_source_url=IDENTIFIER_URL,
            identifier_fetched_at=STAMP, fetch=fetch, now=STAMP,
        )
        market_error = next(item for item in result["source_errors"] if item["endpoint"] == "market_data")
        self.assertEqual(market_error["code"], "PARSE_ERROR")
        self.assertEqual(market_error["phase"], "parse")
        self.assertEqual(result["market_data_diagnostic"]["status"], "SOURCE_UNAVAILABLE")

    def test_fetch_uses_only_the_two_fixed_moex_endpoints(self):
        specification, market = fixtures()
        calls = []

        def fetch(url):
            calls.append(url)
            return json.dumps(specification if "iss/securities/" in url else market).encode()

        result = fetch_moex_bond_facts(
            ISIN, SECID, identifier_source_url=IDENTIFIER_URL,
            identifier_fetched_at=STAMP, fetch=fetch, now=STAMP,
        )
        self.assertEqual(result["status"], "DATA_RECEIVED")
        self.assertEqual(calls, [SPEC_URL, MARKET_URL])

    def test_invalid_json_is_reported_as_parse_error(self):
        result = fetch_moex_bond_facts(
            ISIN, SECID, identifier_source_url=IDENTIFIER_URL,
            identifier_fetched_at=STAMP, fetch=lambda _url: b"not-json", now=STAMP,
        )
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual([item["code"] for item in result["source_errors"]],
                         ["PARSE_ERROR", "PARSE_ERROR"])

    def test_matching_fields_across_description_and_security_tables_are_not_conflicts(self):
        specification, market = fixtures()
        result = parse(specification, market)
        for name in ("face_value", "maturity_date", "coupon_rate_percent", "coupon_amount", "coupon_period_days"):
            self.assertEqual(result["fields"][name]["state"], "CONFIRMED_BY_MOEX", name)
        self.assertEqual(result["fields"]["coupon_period_days"]["source_urls"], [SPEC_URL, MARKET_URL])

    def test_same_coupon_period_with_different_source_values_stays_conflicted(self):
        specification, market = fixtures()
        for row in specification["description"]["data"]:
            if row[0] == "COUPONPERIOD":
                row[2] = "31"
        result = parse(specification, market)
        self.assertEqual(result["fields"]["coupon_period_days"]["state"], "CONFLICT")
        self.assertEqual(result["status"], "CONFLICT")

    def test_invalid_security_id_is_rejected_before_network(self):
        calls = []
        with self.assertRaises(ValueError):
            fetch_moex_bond_facts(
                ISIN, "../OTHER", identifier_source_url=IDENTIFIER_URL,
                identifier_fetched_at=STAMP, fetch=lambda url: calls.append(url) or b"{}",
            )
        self.assertEqual(calls, [])

    def test_transport_failure_is_reported_without_raw_error_text(self):
        def fail(_url):
            raise SourceUnavailable("private transport detail", code="CONNECTION_FAILURE")

        result = fetch_moex_bond_facts(
            ISIN, SECID, identifier_source_url=IDENTIFIER_URL,
            identifier_fetched_at=STAMP, fetch=fail, now=STAMP,
        )
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual([item["code"] for item in result["source_errors"]],
                         ["CONNECTION_FAILURE", "CONNECTION_FAILURE"])
        self.assertNotIn("private transport detail", json.dumps(result))
        for fact in result["fields"].values():
            self.assertTrue(fact["source_urls"])
            self.assertTrue(fact["retrieved_at"])

    def test_every_field_has_provenance_and_explicit_state(self):
        specification, market = fixtures()
        result = parse(specification, market)
        for fact in result["fields"].values():
            self.assertTrue(fact["source_urls"])
            self.assertIn("state", fact)
            if fact["state"].startswith("CONFIRMED"):
                self.assertTrue(fact["retrieved_at"])


if __name__ == "__main__":
    unittest.main()
