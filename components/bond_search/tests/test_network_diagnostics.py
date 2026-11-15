from datetime import datetime, timezone
import json
import socket
import ssl
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from msp_bonds_bond_search.discovery import (
    FetchDiagnostic,
    SourceUnavailable,
    _error_reason,
    lookup_moex_isin,
    safe_get,
)


TEST_ISIN = "RU0000000000"
TEST_URL = "https://iss.moex.com/iss/securities.json?q=" + TEST_ISIN


class NetworkDiagnosticTests(unittest.TestCase):
    def test_transport_layers_are_classified_without_exception_payloads(self):
        self.assertEqual(_error_reason(socket.gaierror("hidden host"), "connect"), ("DNS_FAILURE", True))
        self.assertEqual(_error_reason(ssl.SSLError("hidden cert detail"), "connect"), ("TLS_FAILURE", False))
        self.assertEqual(_error_reason(TimeoutError(), "connect"), ("CONNECT_TIMEOUT", True))
        self.assertEqual(_error_reason(TimeoutError(), "read"), ("READ_TIMEOUT", True))

    def test_safe_get_retries_one_transient_server_failure(self):
        calls = []

        def request_once(url, *, timeout, attempt, observer=None):
            calls.append(attempt)
            if attempt == 1:
                raise SourceUnavailable(
                    "HTTP error", code="HTTP_503", retryable=True,
                    http_status=503, phase="http", attempts=attempt,
                )
            return b"{}"

        with patch("msp_bonds_bond_search.discovery._request_once", side_effect=request_once):
            with patch("msp_bonds_bond_search.discovery.time.sleep"):
                result = safe_get(TEST_URL, max_attempts=2)

        self.assertEqual(result, b"{}")
        self.assertEqual(calls, [1, 2])

    def test_safe_get_does_not_retry_non_transient_error(self):
        calls = []

        def request_once(url, *, timeout, attempt, observer=None):
            calls.append(attempt)
            raise SourceUnavailable(
                "TLS error", code="TLS_FAILURE", retryable=False,
                phase="connect", attempts=attempt,
            )

        with patch("msp_bonds_bond_search.discovery._request_once", side_effect=request_once):
            with self.assertRaises(SourceUnavailable) as caught:
                safe_get(TEST_URL, max_attempts=2)

        self.assertEqual(caught.exception.code, "TLS_FAILURE")
        self.assertEqual(
            caught.exception.diagnostic_summary,
            "TLS_FAILURE phase=connect attempts=1 response_bytes=0",
        )
        self.assertEqual(calls, [1])

    def test_exact_identifier_check_reports_size_and_no_guessed_match(self):
        payload = (
            b'{"securities":{"columns":["ISIN","SECID","SHORTNAME"],'
            b'"data":[["RU0000000000","SYNTHETIC01","Synthetic sample"]]}}'
        )
        result = lookup_moex_isin(
            TEST_ISIN,
            fetch=lambda url: payload,
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertTrue(result.matched)
        self.assertEqual(result.security_id, "SYNTHETIC01")
        self.assertEqual(result.response_bytes, len(payload))
        self.assertEqual(result.attempts, 1)

        empty = b'{"securities":{"columns":["ISIN","SECID"],"data":[]}}'
        missing = lookup_moex_isin(TEST_ISIN, fetch=lambda url: empty)
        self.assertFalse(missing.matched)
        self.assertEqual(missing.state, "INCONCLUSIVE")
        self.assertEqual(missing.security_id, "")

    def test_finder_empty_response_falls_back_to_official_bond_list(self):
        calls = []
        finder_empty = b'{"securities":{"columns":["ISIN","SECID"],"data":[]}}'
        listing = (
            b'{"securities":{"columns":["ISIN","SECID","SHORTNAME"],'
            b'"data":[["RU0000000000","SYNTHETIC01","Synthetic sample"]]}}'
        )

        def fetch(url):
            calls.append(url)
            return finder_empty if "q=" in url else listing

        result = lookup_moex_isin(TEST_ISIN, fetch=fetch)
        self.assertTrue(result.matched)
        self.assertEqual(result.security_id, "SYNTHETIC01")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.response_bytes, len(finder_empty) + len(listing))
        self.assertIn("/engines/stock/markets/bonds/securities.json", result.source_url)
        self.assertEqual(len(calls), 2)

    @staticmethod
    def _iss_page(rows, *, index, total, page_size):
        return json.dumps({
            "securities": {
                "columns": ["ISIN", "SECID", "SHORTNAME"],
                "data": rows,
            },
            "securities.cursor": {
                "columns": ["INDEX", "TOTAL", "PAGESIZE"],
                "data": [[index, total, page_size]],
            },
        }).encode("utf-8")

    def test_finder_empty_then_exact_isin_on_second_listing_page(self):
        finder = b'{"securities":{"columns":["ISIN","SECID"],"data":[]}}'
        first = self._iss_page([["RU0000000001", "SYNTHETIC01", "Synthetic one"]], index=0, total=2, page_size=1)
        second = self._iss_page([[TEST_ISIN, "SYNTHETIC02", "Synthetic two"]], index=1, total=2, page_size=1)
        calls = []

        def fetch(url):
            calls.append(url)
            if "q=" in url:
                return finder
            start = int(parse_qs(urlsplit(url).query)["start"][0])
            return first if start == 0 else second

        result = lookup_moex_isin(TEST_ISIN, fetch=fetch)
        self.assertTrue(result.matched)
        self.assertEqual(result.security_id, "SYNTHETIC02")
        self.assertEqual(result.scanned_pages, 2)
        self.assertEqual(result.scanned_rows, 2)
        self.assertTrue(result.scan_complete)  # Cursor proves the second page is final.
        self.assertEqual(len(calls), 3)

    def test_partial_listing_without_cursor_is_inconclusive_not_absent(self):
        finder = b'{"securities":{"columns":["ISIN","SECID"],"data":[]}}'
        partial = b'{"securities":{"columns":["ISIN","SECID"],"data":[["RU0000000001","SYNTHETIC01"]]}}'
        calls = []

        def fetch(url):
            calls.append(url)
            return finder if "q=" in url else partial

        result = lookup_moex_isin(TEST_ISIN, fetch=fetch)
        self.assertFalse(result.matched)
        self.assertEqual(result.state, "INCONCLUSIVE")
        self.assertFalse(result.scan_complete)
        self.assertEqual(result.scanned_pages, 1)
        self.assertEqual(result.scanned_rows, 1)

    def test_repeated_page_with_ignored_start_is_inconclusive(self):
        finder = b'{"securities":{"columns":["ISIN","SECID"],"data":[]}}'
        repeated = self._iss_page([["RU0000000001", "SYNTHETIC01", "Synthetic one"]], index=0, total=2, page_size=1)
        calls = []

        def fetch(url):
            calls.append(url)
            return finder if "q=" in url else repeated

        result = lookup_moex_isin(TEST_ISIN, fetch=fetch)
        self.assertEqual(result.state, "INCONCLUSIVE")
        self.assertFalse(result.scan_complete)
        self.assertEqual(result.scanned_pages, 2)
        self.assertEqual(len(calls), 3)

    def test_exhausted_cursor_returns_only_scanned_results_status(self):
        finder = b'{"securities":{"columns":["ISIN","SECID"],"data":[]}}'
        page = self._iss_page([["RU0000000001", "SYNTHETIC01", "Synthetic one"]], index=0, total=1, page_size=1)
        result = lookup_moex_isin(TEST_ISIN, fetch=lambda url: finder if "q=" in url else page)
        self.assertFalse(result.matched)
        self.assertEqual(result.state, "NOT_FOUND_IN_SCANNED_RESULTS")
        self.assertTrue(result.scan_complete)
        self.assertEqual(result.scanned_pages, 1)
        self.assertEqual(result.scanned_rows, 1)
        self.assertIn("total=1;complete=true", result.scan_range)

    def test_listing_timeout_is_not_converted_into_a_negative_match(self):
        finder = b'{"securities":{"columns":["ISIN","SECID"],"data":[]}}'

        def fetch(url):
            if "q=" in url:
                return finder
            raise SourceUnavailable("timeout", code="READ_TIMEOUT", retryable=True, phase="read")

        with self.assertRaises(SourceUnavailable) as caught:
            lookup_moex_isin(TEST_ISIN, fetch=fetch)
        self.assertEqual(caught.exception.code, "READ_TIMEOUT")

    def test_bad_json_is_a_parse_error(self):
        with self.assertRaises(SourceUnavailable) as caught:
            lookup_moex_isin(TEST_ISIN, fetch=lambda url: b"not-json")
        self.assertEqual(caught.exception.code, "PARSE_ERROR")


if __name__ == "__main__":
    unittest.main()
