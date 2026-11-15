from datetime import datetime, timezone
import socket
import ssl
import unittest
from unittest.mock import patch

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
        self.assertEqual(missing.state, "NO_EXACT_MATCH_IN_CURRENT_BOND_LIST")
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

    def test_bad_json_is_a_parse_error(self):
        with self.assertRaises(SourceUnavailable) as caught:
            lookup_moex_isin(TEST_ISIN, fetch=lambda url: b"not-json")
        self.assertEqual(caught.exception.code, "PARSE_ERROR")


if __name__ == "__main__":
    unittest.main()
