"""Synthetic Smart-Lab HTML tests; tests never access public services."""
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from unittest.mock import Mock

from msp_bonds_bond_search.discovery import FetchDiagnostic, SourceUnavailable
from msp_bonds_bond_search.policy import SelectionPolicy
from msp_bonds_bond_search.smartlab_screen import (
    SMARTLAB_LISTING_URL,
    SmartLabSearchResult,
    assess_preliminary,
    discover_smartlab_pages,
    make_search_report,
    parse_smartlab_listing,
)
from msp_bonds_bond_search.smartlab_curl import _CurlResponse
from policy_fixtures import snapshot

NOW = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)


def row(rank, isin, name, *, rating="A-", offer="-"):
    values = [
        str(rank), name, "", "1.2", "12.3%", "10.0%", "10.1%", rating,
        "100", "2.1", "15.6", "12", "1.2", "0.8", "99.5",
        "15.12.26", "01.01.25", "01.01.28", offer, "+", "+",
    ]
    cells = "".join(f"<td>{value}</td>" for value in values)
    cells = cells.replace(
        f"<td>{name}</td>",
        f'<td><a href="/q/bonds/{isin}/">{name}</a></td>',
        1,
    )
    return f"<tr>{cells}</tr>"


HEADERS = [
    "№", "Имя", "", "Лет до погаш.", "Доходн", "Год.куп.дох.", "Куп.дох.посл.",
    "Рейтинг", "G-спред", "Объеммлн руб", "Купон, руб", "Частота,раз в год",
    "НКД, руб", "Дюр-я, лет", "Цена", "Дата купона", "Размещение",
    "Погашение", "Оферта", "Флаг 1", "Флаг 2",
]


def page(*rows, pagination=""):
    header = "".join(f"<th>{value}</th>" for value in HEADERS)
    return f"<table><tr>{header}</tr>{''.join(rows)}</table>{pagination}"


class SmartLabListingTests(unittest.TestCase):
    def test_parses_financial_fields_and_keeps_rating_preliminary(self):
        html = page(row(1, "RU000A123456", "Тест 1Р1"))
        bonds, warnings, _ = parse_smartlab_listing(html, fetched_at=NOW)
        self.assertFalse(warnings)
        self.assertEqual(len(bonds), 1)
        bond = bonds[0]
        self.assertEqual(bond.isin, "RU000A123456")
        self.assertEqual(bond.name, "Тест 1Р1")
        self.assertEqual(bond.fields["reported_yield_percent"], Decimal("12.3"))
        self.assertEqual(bond.fields["price_percent_of_nominal"], Decimal("99.5"))
        self.assertEqual(bond.fields["coupon_rub"], Decimal("15.6"))
        self.assertEqual(bond.fields["accrued_interest_rub"], Decimal("1.2"))
        self.assertEqual(bond.fields["duration_years"], Decimal("0.8"))
        self.assertEqual(bond.fields["maturity_date"], date(2028, 1, 1))
        self.assertEqual(bond.fields["rating_preliminary"], "A-")
        self.assertEqual(bond.listing_url, SMARTLAB_LISTING_URL)
        self.assertIn("source_url", bond.to_dict())
        self.assertNotIn("https://smart-lab.ru/q/bonds/RU000A123456/", bond.to_dict()["source_url"])

    def test_missing_cells_are_none_and_schema_is_reported(self):
        html = page(row(1, "RU000A123456", "Без купона", rating="-", offer="-"))
        bonds, warnings, _ = parse_smartlab_listing(html.replace("<td>12.3%</td>", "<td>-</td>"), fetched_at=NOW)
        self.assertIsNone(bonds[0].fields["reported_yield_percent"])
        self.assertIsNone(bonds[0].fields["rating_preliminary"])
        self.assertFalse(warnings)

    def test_changed_required_header_fails_closed(self):
        html = page(row(1, "RU000A123456", "Серия" )).replace("<th>Имя</th>", "<th>Выпуск</th>")
        with self.assertRaises(SourceUnavailable) as caught:
            parse_smartlab_listing(html, fetched_at=NOW)
        self.assertEqual(caught.exception.code, "HTML_SCHEMA_CHANGED")

    def test_removed_optional_header_is_reported_instead_of_shifting_column_values(self):
        html = page(row(1, "RU000A123456", "Тест")).replace("<th>НКД, руб</th>", "<th>НКД снят</th>")
        bonds, warnings, _ = parse_smartlab_listing(html, fetched_at=NOW)
        self.assertIn("accrued_interest_rub", warnings)
        self.assertIn("accrued_interest_rub", bonds[0].missing_columns)
        self.assertIsNone(bonds[0].fields["accrued_interest_rub"])

    def test_page_collection_is_sequential_and_deduplicates_without_detail_fetches(self):
        page_two = "https://smart-lab.ru/q/bonds/order_by_val_to_day/desc/page2/"
        html_one = page(
            row(1, "RU000A123456", "Первая"),
            pagination=f'<nav><a href="{page_two}">2</a></nav>',
        ).encode()
        html_two = page(
            row(2, "RU000A123456", "Первая"),
            row(3, "RU000A654321", "Вторая", rating="BBB+"),
            row(4, "RU000A777777", "Третья", rating="BBB−"),
        ).encode()
        calls = []

        def fake_fetch(url):
            calls.append(url)
            if url == "https://smart-lab.ru/robots.txt":
                return b"User-agent: *\nAllow: /q/bonds/\n"
            if url == SMARTLAB_LISTING_URL:
                return html_one
            if url == page_two:
                return html_two
            raise AssertionError(f"unexpected fetch (detail-page access is forbidden): {url}")

        result = discover_smartlab_pages(fetch=fake_fetch, now=NOW, max_items=3)
        self.assertEqual(result.status, "LIMIT_REACHED")
        self.assertEqual(result.pages_fetched, 2)
        self.assertEqual(
            [item.isin for item in result.bonds],
            ["RU000A123456", "RU000A654321", "RU000A777777"],
        )
        self.assertEqual(result.duplicate_rows, 1)
        self.assertEqual(calls.count(page_two), 1)
        self.assertTrue(all("/q/bonds/" in url or url.endswith("robots.txt") for url in calls))
        report = make_search_report(
            result, SelectionPolicy.from_snapshot(snapshot(fetched_at=NOW)),
        )
        self.assertEqual(report["listing_pages_fetched"], 2)
        self.assertEqual(report["preliminary_summary"]["candidate_count"], 3)
        self.assertEqual(
            sum(report["preliminary_summary"]["status_counts"].values()),
            3,
        )
        matrix = report["preliminary_summary"]["rating_status_counts"]
        self.assertEqual(
            matrix["A_and_higher_including_A_minus"]["ПРЕДВАРИТЕЛЬНО ПОДХОДИТ"],
            1,
        )
        self.assertEqual(
            matrix["BBB_and_BBB_plus"]["ПРЕДВАРИТЕЛЬНО ПОДХОДИТ"],
            1,
        )
        self.assertEqual(
            matrix["below_BBB_preliminary"]["НЕ ПОДХОДИТ"],
            1,
        )
        self.assertEqual(
            sum(sum(status_counts.values()) for status_counts in matrix.values()),
            3,
        )
        self.assertIn("volume_mln_rub", report["field_interpretation"])

    def test_full_collection_follows_observed_navigation_until_short_final_page(self):
        page_two = "https://smart-lab.ru/q/bonds/order_by_val_to_day/desc/page2/"
        page_one_html = page(
            row(1, "RU000A123456", "Первая"),
            row(2, "RU000A654321", "Вторая", rating="BBB+"),
            pagination=f'<nav><a href="{page_two}">2</a></nav>',
        ).encode()
        page_two_html = page(
            row(3, "RU000A777777", "Третья"),
        ).encode()
        calls = []

        def fake_fetch(url):
            calls.append(url)
            if url.endswith("robots.txt"):
                return b"User-agent: *\nAllow: /q/bonds/\n"
            if url == SMARTLAB_LISTING_URL:
                return page_one_html
            if url == page_two:
                return page_two_html
            raise AssertionError(f"unexpected page request: {url}")

        result = discover_smartlab_pages(fetch=fake_fetch, now=NOW, max_items=None)
        self.assertEqual(result.status, "COMPLETE")
        self.assertTrue(result.collect_all)
        self.assertIsNone(result.requested_max_items)
        self.assertEqual(result.pages_fetched, 2)
        self.assertEqual([bond.source_row_number for bond in result.bonds], [1, 2, 3])
        self.assertEqual(len(set(bond.isin for bond in result.bonds)), 3)
        self.assertEqual(calls, ["https://smart-lab.ru/robots.txt", SMARTLAB_LISTING_URL, page_two])
        self.assertTrue(result.row_sequence_verified)
        self.assertEqual(result.sort_signature, "val_to_day:desc")

        report = make_search_report(
            result, SelectionPolicy.from_snapshot(snapshot(fetched_at=NOW)),
        )
        self.assertTrue(report["pagination"]["full_scan_complete"])
        self.assertEqual(report["pagination"]["unique_isins"], 3)
        self.assertEqual(report["candidates"][0]["source_row_number"], 1)

    def test_full_sized_final_page_can_end_with_previous_page_links_only(self):
        page_two = "https://smart-lab.ru/q/bonds/order_by_val_to_day/desc/page2/"
        first_rows = [row(index, f"RU000A{index:06d}", f"Выпуск {index}") for index in range(1, 101)]
        second_rows = [row(index, f"RU000A{index:06d}", f"Выпуск {index}") for index in range(101, 201)]
        first_page = page(
            *first_rows,
            pagination=f'<a href="{page_two}">2</a>',
        ).encode()
        second_page = page(
            *second_rows,
            pagination=f'<a href="{SMARTLAB_LISTING_URL}">1</a>',
        ).encode()

        def fake_fetch(url):
            if url.endswith("robots.txt"):
                return b"User-agent: *\nAllow: /q/bonds/\n"
            if url == SMARTLAB_LISTING_URL:
                return first_page
            if url == page_two:
                return second_page
            raise AssertionError(f"unexpected page request: {url}")

        result = discover_smartlab_pages(fetch=fake_fetch, now=NOW, max_items=None)
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(result.pages_fetched, 2)
        self.assertEqual(len(result.bonds), 200)
        self.assertTrue(result.row_sequence_verified)

    def test_full_collection_marks_row_gaps_duplicates_and_sort_changes_incomplete(self):
        page_two = "https://smart-lab.ru/q/bonds/order_by_val_to_day/desc/page2/"
        cases = (
            (
                "row_gap",
                page(row(1, "RU000A123456", "Первая"), pagination=f'<a href="{page_two}">2</a>'),
                page(row(3, "RU000A654321", "Вторая")),
                "ROW_NUMBER_SEQUENCE_MISMATCH_PAGE_2",
                "INTEGRITY_CHECK_FAILED",
            ),
            (
                "duplicate_isin",
                page(row(1, "RU000A123456", "Первая"), pagination=f'<a href="{page_two}">2</a>'),
                page(row(2, "RU000A123456", "Повтор")),
                "DUPLICATE_ISIN_ROWS_DETECTED",
                "DUPLICATE_ISIN_ROWS_DETECTED",
            ),
        )
        for name, first_page, second_page, expected_issue, expected_status in cases:
            with self.subTest(case=name):
                first_url = SMARTLAB_LISTING_URL
                next_url = page_two
                first_page_bytes = first_page.encode()
                def fake_fetch(url):
                    if url.endswith("robots.txt"):
                        return b"User-agent: *\nAllow: /q/bonds/\n"
                    if url == first_url:
                        return first_page_bytes
                    if url == next_url:
                        return second_page.encode()
                    raise AssertionError(f"unexpected page request: {url}")

                result = discover_smartlab_pages(fetch=fake_fetch, now=NOW, max_items=None)
                self.assertEqual(result.status, expected_status)
                self.assertIn(expected_issue, result.integrity_issues)

    def test_full_collection_detects_sort_signature_change_between_pages(self):
        page_two = "https://smart-lab.ru/q/bonds/order_by_val_to_day/desc/page2/"
        page_three_other_sort = "https://smart-lab.ru/q/bonds/order_by_duration/desc/page3/"
        first_page = page(
            row(1, "RU000A123456", "Первая"),
            pagination=f'<a href="{page_two}">2</a>',
        ).encode()
        second_page = page(
            row(2, "RU000A654321", "Вторая"),
            pagination=f'<a href="{page_three_other_sort}">3</a>',
        ).encode()
        third_page = page(row(3, "RU000A777777", "Третья")).encode()

        def fake_fetch(url):
            if url.endswith("robots.txt"):
                return b"User-agent: *\nAllow: /q/bonds/\n"
            if url == SMARTLAB_LISTING_URL:
                return first_page
            if url == page_two:
                return second_page
            if url == page_three_other_sort:
                return third_page
            raise AssertionError(f"unexpected page request: {url}")

        result = discover_smartlab_pages(fetch=fake_fetch, now=NOW, max_items=None)
        self.assertEqual(result.status, "INTEGRITY_CHECK_FAILED")
        self.assertIn("SORT_SIGNATURE_CHANGED_PAGE_2", result.integrity_issues)

    def test_page_safety_limit_is_reported_as_incomplete_not_complete(self):
        page_two = "https://smart-lab.ru/q/bonds/order_by_val_to_day/desc/page2/"

        def fake_fetch(url):
            if url.endswith("robots.txt"):
                return b"User-agent: *\nAllow: /q/bonds/\n"
            if url == SMARTLAB_LISTING_URL:
                return page(row(1, "RU000A123456", "Первая"), pagination=f'<a href="{page_two}">2</a>').encode()
            raise AssertionError(f"page safety limit should prevent requesting: {url}")

        with patch("msp_bonds_bond_search.smartlab_screen._MAX_PAGE_REQUESTS", 1):
            result = discover_smartlab_pages(fetch=fake_fetch, now=NOW, max_items=None)
        self.assertEqual(result.status, "PAGE_REQUEST_LIMIT_REACHED")
        self.assertFalse(make_search_report(
            result, SelectionPolicy.from_snapshot(snapshot(fetched_at=NOW)),
        )["pagination"]["full_scan_complete"])
        self.assertIn("PAGE_REQUEST_SAFETY_LIMIT_REACHED", result.integrity_issues)

    def test_each_candidate_contains_the_five_distinct_criteria_and_completeness(self):
        bonds, _, _ = parse_smartlab_listing(page(row(1, "RU000A123456", "Проверка", rating="A-")), fetched_at=NOW)
        policy = SelectionPolicy.from_snapshot(snapshot(fetched_at=NOW))
        report_result = SmartLabSearchResult(
            bonds=tuple(bonds), status="COMPLETE", requested_max_items=None,
            pages_fetched=1, duplicate_rows=0, rows_without_isin=0,
            schema_warnings=(), retrieved_at=NOW, source_diagnostics={}, collect_all=True,
            integrity_issues=(), row_sequence_verified=True, sort_signature="val_to_day:desc",
        )
        result = make_search_report(report_result, policy)
        candidate = result["candidates"][0]
        criteria = candidate["criteria"]
        self.assertEqual(
            set(criteria), {"credit_rating", "yield", "maturity", "liquidity", "coupon"},
        )
        self.assertEqual(criteria["credit_rating"]["screening_group"], "A_minus_or_higher_priority")
        self.assertEqual(criteria["yield"]["published_yield_percent"], "12.3")
        self.assertEqual(criteria["yield"]["annual_coupon_yield_percent"], "10.0")
        self.assertEqual(criteria["yield"]["last_coupon_yield_percent"], "10.1")
        self.assertEqual(criteria["yield"]["g_spread"], "100")
        self.assertFalse(criteria["maturity"]["offer_is_guaranteed_maturity"])
        self.assertFalse(criteria["liquidity"]["is_confirmed_liquidity_measure"])
        self.assertIsNone(criteria["liquidity"]["bid_ask_spread"])
        self.assertTrue(criteria["coupon"]["terms_verification_required"])
        completeness = result["preliminary_summary"]["criterion_completeness"]
        self.assertEqual(completeness["yield"]["field_counts"]["g_spread_bp"], 1)
        self.assertEqual(completeness["liquidity"]["volume_period_known"], 0)

    def test_explicit_coupon_field_conflict_is_separate_and_requires_clarification(self):
        html = page(row(1, "RU000A123456", "Конфликт", rating="A-"))
        html = html.replace("<td>12</td>", "<td>0</td>", 1)
        bonds, _, _ = parse_smartlab_listing(html, fetched_at=NOW)
        policy = SelectionPolicy.from_snapshot(snapshot(fetched_at=NOW))
        assessment = assess_preliminary(bonds[0], policy)
        self.assertEqual(assessment.status, "ТРЕБУЕТ УТОЧНЕНИЯ")
        self.assertIn("smartlab_coupon_fields_have_incompatible_values", assessment.decision_blockers)
        report_result = SmartLabSearchResult(
            bonds=tuple(bonds), status="COMPLETE", requested_max_items=1,
            pages_fetched=1, duplicate_rows=0, rows_without_isin=0,
            schema_warnings=(), retrieved_at=NOW, source_diagnostics={}, collect_all=False,
            integrity_issues=(), row_sequence_verified=True, sort_signature=None,
        )
        criteria = make_search_report(report_result, policy)["candidates"][0]["criteria"]["coupon"]
        self.assertIn("positive_coupon_amount_with_zero_or_negative_frequency", criteria["consistency_flags"])

    def test_robots_policy_is_checked_before_listing(self):
        calls = []

        def fake_fetch(url):
            calls.append(url)
            return b"User-agent: *\nDisallow: /q/bonds/\n"

        with self.assertRaises(SourceUnavailable) as caught:
            discover_smartlab_pages(fetch=fake_fetch, now=NOW, max_items=1)
        self.assertEqual(caught.exception.code, "ROBOTS_DISALLOWED")
        self.assertEqual(calls, ["https://smart-lab.ru/robots.txt"])

    def test_listing_timeout_retries_and_reports_robots_and_page_attempts(self):
        html = page(row(1, "RU000A123456", "Первая")).encode()
        calls = []

        def curl_once(url, *, attempt, conditional_headers, observer=None):
            calls.append((url, attempt))
            if url.endswith("/robots.txt"):
                payload = b"User-agent: *\nAllow: /q/bonds/\n"
                return _CurlResponse(payload, 200, len(payload), {}, 0.02, 0)
            if url == SMARTLAB_LISTING_URL and attempt == 1:
                if observer:
                    observer(FetchDiagnostic(
                        "smart-lab.ru", "read", attempt, 0.03,
                        0, 200, "READ_TIMEOUT", url,
                    ))
                raise SourceUnavailable(
                    "timed out", code="READ_TIMEOUT", retryable=True,
                    http_status=200, phase="read", attempts=attempt,
                    source_url=url,
                )
            if url == SMARTLAB_LISTING_URL:
                return _CurlResponse(html, 200, len(html), {}, 0.04, 0)
            raise AssertionError(f"unexpected request: {url}")

        cache = Mock()
        cache.load.return_value = None
        with patch("msp_bonds_bond_search.smartlab_curl._run_curl", side_effect=curl_once):
            with patch("msp_bonds_bond_search.smartlab_curl._default_cache", return_value=cache):
                with patch("msp_bonds_bond_search.smartlab_curl.time.sleep"):
                    result = discover_smartlab_pages(now=NOW, max_items=1)

        self.assertEqual(len(result.bonds), 1)
        self.assertEqual(result.pages_fetched, 1)
        self.assertEqual(calls.count(("https://smart-lab.ru/robots.txt", 1)), 1)
        self.assertEqual(calls.count((SMARTLAB_LISTING_URL, 1)), 1)
        self.assertEqual(calls.count((SMARTLAB_LISTING_URL, 2)), 1)
        source = result.source_diagnostics
        self.assertEqual(source["robots"]["status"], "ALLOWED")
        page_result = source["listing_pages"][0]
        self.assertEqual(page_result["url"], SMARTLAB_LISTING_URL)
        self.assertEqual(page_result["status"], "PARSED")
        self.assertEqual([item["attempt"] for item in page_result["attempts"]], [1, 2])
        self.assertEqual(page_result["attempts"][0]["error_code"], "READ_TIMEOUT")
        self.assertEqual(page_result["attempts"][0]["http_status"], 200)
        self.assertGreater(page_result["response_bytes"], 0)

    def test_terminal_first_page_timeout_names_exact_url_and_robot_result(self):
        def curl_once(url, *, attempt, conditional_headers, observer=None):
            if url.endswith("/robots.txt"):
                payload = b"User-agent: *\nAllow: /q/bonds/\n"
                return _CurlResponse(payload, 200, len(payload), {}, 0.01, 0)
            if observer:
                observer(FetchDiagnostic(
                    "smart-lab.ru", "read", attempt, 0.02,
                    245389, 200, "READ_TIMEOUT", url,
                    content_length=253046,
                    content_encoding="identity",
                    body_framing="content-length",
                    body_complete=False,
                ))
            raise SourceUnavailable(
                "timed out", code="READ_TIMEOUT", retryable=True,
                http_status=200, phase="read", attempts=attempt,
                source_url=url,
            )

        cache = Mock()
        cache.load.return_value = None
        with patch("msp_bonds_bond_search.smartlab_curl._run_curl", side_effect=curl_once):
            with patch("msp_bonds_bond_search.smartlab_curl._default_cache", return_value=cache):
                with patch("msp_bonds_bond_search.smartlab_curl.time.sleep"):
                    with self.assertRaises(SourceUnavailable) as caught:
                        discover_smartlab_pages(now=NOW, max_items=1)

        self.assertEqual(caught.exception.source_url, SMARTLAB_LISTING_URL)
        self.assertIn(f"url={SMARTLAB_LISTING_URL}", caught.exception.diagnostic_summary)
        self.assertIn("robots=ALLOWED,http=200,bytes=", caught.exception.diagnostic_summary)
        self.assertIn(f"page1=FETCH_FAILED,http=200,bytes=0@{SMARTLAB_LISTING_URL}", caught.exception.diagnostic_summary)
        self.assertIn(
            "listing_page_fetch_try2=READ_TIMEOUT@",
            caught.exception.diagnostic_summary,
        )
        self.assertIn("bytes=245389,cl=253046", caught.exception.diagnostic_summary)
        self.assertIn("framing=content-length,complete=False", caught.exception.diagnostic_summary)

    def test_later_page_failure_keeps_first_page_candidates_and_marks_partial(self):
        page_two = "https://smart-lab.ru/q/bonds/order_by_val_to_day/desc/page2/"
        first = page(
            row(1, "RU000A123456", "Первая"),
            pagination=f'<nav><a href="{page_two}">2</a></nav>',
        ).encode()

        def fake_fetch(url):
            if url.endswith("robots.txt"):
                return (
                    b"User-agent: *\n"
                    b"Disallow: /q/bonds/order_by_val_to_day/\n"
                    b"Allow: /q/bonds/\n"
                )
            if url == SMARTLAB_LISTING_URL:
                return first
            raise AssertionError("second listing page must be blocked by robots")

        calls = []

        def tracked_fetch(url):
            calls.append(url)
            return fake_fetch(url)

        result = discover_smartlab_pages(fetch=tracked_fetch, now=NOW, max_items=2)
        self.assertEqual(result.status, "PARTIAL_SOURCE_FAILURE")
        self.assertEqual(len(result.bonds), 1)
        self.assertIn("ROBOTS_DISALLOWED", result.source_error)
        self.assertEqual(calls, ["https://smart-lab.ru/robots.txt", SMARTLAB_LISTING_URL])
        self.assertEqual(result.source_diagnostics["robots"]["status"], "DISALLOWED")
        self.assertEqual(result.source_diagnostics["listing_pages"][1]["status"], "BLOCKED_BY_ROBOTS")

    def test_rows_without_isin_are_not_reported_as_a_complete_candidate_set(self):
        raw_row = "<tr>" + "".join(f"<td>{v}</td>" for v in [
            "1", "Без кода", "", "1.2", "12.3%", "10%", "10%", "A-", "100",
            "2.1", "15.6", "12", "1.2", "0.8", "99.5", "15.12.26", "01.01.25",
            "01.01.28", "-", "+", "+",
        ]) + "</tr>"
        calls = []

        def fake_fetch(url):
            calls.append(url)
            if url.endswith("robots.txt"):
                return b"User-agent: *\nAllow: /q/bonds/\n"
            return page(raw_row).encode()

        result = discover_smartlab_pages(fetch=fake_fetch, now=NOW, max_items=1)
        self.assertEqual(result.status, "NO_ISIN_IDENTIFIERS")
        self.assertEqual(result.rows_without_isin, 1)
        self.assertEqual(result.bonds, ())

    def test_initial_fit_is_not_blocked_by_follow_up_official_checks(self):
        bonds, _, _ = parse_smartlab_listing(page(row(1, "RU000A123456", "Тест")), fetched_at=NOW)
        policy = SelectionPolicy.from_snapshot(snapshot(fetched_at=NOW))
        assessment = assess_preliminary(bonds[0], policy)
        self.assertEqual(assessment.status, "ПРЕДВАРИТЕЛЬНО ПОДХОДИТ")
        self.assertFalse(assessment.decision_blockers)
        self.assertIn("official_rating_confirmation_smartlab_rating_is_preliminary", assessment.needs_verification)
        self.assertIn("issuer_country_confirmation_from_official_record", assessment.needs_verification)
        self.assertIn("ruble_currency_confirmation_from_issue_record", assessment.needs_verification)
        self.assertIn("instrument_classification_from_official_security_record", assessment.needs_verification)
        self.assertIn("future_coupon_terms_and_offer_mechanisms_from_issue_documents", assessment.needs_verification)
        self.assertIn("no_fixed_minimum_yield_or_nominal_limit_applied", assessment.preferences)

    def test_missing_or_unrecognized_preliminary_rating_needs_clarification(self):
        policy = SelectionPolicy.from_snapshot(snapshot(fetched_at=NOW))
        for rating in ("-", "неизвестно"):
            with self.subTest(rating=rating):
                bonds, _, _ = parse_smartlab_listing(
                    page(row(1, "RU000A123456", "Без рейтинга", rating=rating)),
                    fetched_at=NOW,
                )
                assessment = assess_preliminary(bonds[0], policy)
                self.assertEqual(assessment.status, "ТРЕБУЕТ УТОЧНЕНИЯ")
                self.assertIn(
                    "rating_missing_or_unrecognized_for_initial_floor_check",
                    assessment.decision_blockers,
                )

    def test_explicit_noncorporate_type_or_nonruble_currency_is_rejected(self):
        bonds, _, _ = parse_smartlab_listing(
            page(row(1, "RU000A123456", "Тест", rating="A-")),
            fetched_at=NOW,
        )
        policy = SelectionPolicy.from_snapshot(snapshot(fetched_at=NOW))
        ofz = assess_preliminary(
            bonds[0], policy, issuer_country="RU", currency="RUB",
            instrument_type="ОФЗ",
        )
        foreign_currency = assess_preliminary(
            bonds[0], policy, issuer_country="RU", currency="USD",
            instrument_type="корпоративная облигация",
        )
        self.assertEqual(ofz.status, "НЕ ПОДХОДИТ")
        self.assertIn("instrument_not_corporate_bond", ofz.reasons)
        self.assertEqual(foreign_currency.status, "НЕ ПОДХОДИТ")
        self.assertIn("currency_not_rub", foreign_currency.reasons)

    def test_preferred_maturity_is_not_a_rejection_and_a_minus_has_no_five_unit_cap(self):
        bonds, _, _ = parse_smartlab_listing(page(row(1, "RU000A123456", "Тест", rating="A-")), fetched_at=NOW)
        policy = SelectionPolicy.from_snapshot(snapshot(fetched_at=NOW))
        assessment = assess_preliminary(
            bonds[0], policy, issuer_country="RU", currency="RUB",
            official_rating="A-", official_rating_confirmed=True,
            future_coupon_terms_confirmed=True,
        )
        self.assertNotEqual(assessment.status, "НЕ ПОДХОДИТ")
        self.assertFalse(any("bbb_issuer" in check for check in assessment.needs_verification))

    def test_recognized_preliminary_below_floor_is_rejected_and_confirmation_remains_required(self):
        bonds, _, _ = parse_smartlab_listing(
            page(row(1, "RU000A123456", "Тест", rating="BBB−")), fetched_at=NOW,
        )
        policy = SelectionPolicy.from_snapshot(snapshot(fetched_at=NOW))
        preliminary = assess_preliminary(bonds[0], policy)
        self.assertEqual(preliminary.status, "НЕ ПОДХОДИТ")
        self.assertIn("preliminary_rating_below_approved_minimum", preliminary.reasons)
        self.assertIn(
            "official_rating_confirmation_smartlab_rating_is_preliminary",
            preliminary.needs_verification,
        )
        confirmed = assess_preliminary(
            bonds[0], policy, issuer_country="RU", currency="RUB",
            official_rating="BBB-", official_rating_confirmed=True,
        )
        self.assertEqual(confirmed.status, "НЕ ПОДХОДИТ")
        self.assertIn("confirmed_rating_below_approved_minimum", confirmed.reasons)

    def test_rating_boundary_grades_bbb_minus_bbb_bbb_plus_and_a_minus(self):
        policy = SelectionPolicy.from_snapshot(snapshot(fetched_at=NOW))
        expectations = {
            "BBB−": "НЕ ПОДХОДИТ",
            "BBB": "ПРЕДВАРИТЕЛЬНО ПОДХОДИТ",
            "BBB+": "ПРЕДВАРИТЕЛЬНО ПОДХОДИТ",
            "A−": "ПРЕДВАРИТЕЛЬНО ПОДХОДИТ",
        }
        for rating, expected in expectations.items():
            with self.subTest(rating=rating):
                bonds, _, _ = parse_smartlab_listing(
                    page(row(1, "RU000A123456", "Тест", rating=rating)),
                    fetched_at=NOW,
                )
                assessment = assess_preliminary(bonds[0], policy)
                self.assertEqual(assessment.status, expected)

    def test_maturity_preference_uses_maturity_not_duration_and_keeps_volume_separate(self):
        bonds, _, _ = parse_smartlab_listing(
            page(row(1, "RU000A123456", "Тест", rating="A-", offer="01.07.27"))
            .replace("<td>01.01.28</td>", "<td>01.01.31</td>"),
            fetched_at=NOW,
        )
        policy = SelectionPolicy.from_snapshot(snapshot(fetched_at=NOW))
        result = assess_preliminary(
            bonds[0], policy,
            issuer_country="RU", currency="RUB", official_rating="A-",
            official_rating_confirmed=True, corporate_instrument_confirmed=True,
            future_coupon_terms_confirmed=True, liquidity_reviewed=True,
        )
        self.assertEqual(result.status, "ПРЕДВАРИТЕЛЬНО ПОДХОДИТ")
        self.assertIn("maturity_over_three_years_is_preference_deviation_not_rejection", result.preferences)
        self.assertIn("duration_is_reported_separately_and_not_used_as_maturity_horizon", result.selection_signals)
        self.assertIn("reported_volume_is_not_a_liquidity_measure", result.selection_signals)
        self.assertNotIn("bid_ask_spread_and_order_book_depth_not_in_listing", result.needs_verification)
        self.assertTrue(result.selection_signals)


if __name__ == "__main__":
    unittest.main()
