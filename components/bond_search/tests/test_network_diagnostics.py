from datetime import datetime, timezone
import http.client
import json
import socket
import ssl
import time
import unittest
from unittest.mock import Mock, patch
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
            f"TLS_FAILURE phase=connect attempts=1 response_bytes=0 url={TEST_URL}",
        )
        self.assertEqual(calls, [1])

    def test_safe_get_streams_partial_body_and_retries_read_timeout(self):
        class Response:
            status = 200

            def __init__(self, values):
                self.values = iter(values)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return TEST_URL

            def read1(self, _amount):
                value = next(self.values)
                if isinstance(value, BaseException):
                    raise value
                return value

        opener = Mock()
        opener.open.side_effect = [
            Response([b"first-chunk", TimeoutError("read stalled")]),
            Response([b"complete-body", b""]),
        ]
        events = []
        with patch("msp_bonds_bond_search.discovery.build_opener", return_value=opener):
            with patch("msp_bonds_bond_search.discovery._rate_limit"):
                with patch("msp_bonds_bond_search.discovery.time.sleep"):
                    payload = safe_get(TEST_URL, max_attempts=2, observer=events.append)

        self.assertEqual(payload, b"complete-body")
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(events[0].reason_code, "READ_TIMEOUT")
        self.assertEqual(events[0].response_bytes, len(b"first-chunk"))
        self.assertEqual(events[0].http_status, 200)
        self.assertEqual(events[0].url, TEST_URL)
        self.assertEqual(events[1].response_bytes, len(b"complete-body"))
        self.assertEqual(events[1].reason_code, "")

    def test_safe_get_counts_incomplete_chunk_bytes_before_successful_retry(self):
        class Response:
            status = 200

            def __init__(self, values):
                self.values = iter(values)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return TEST_URL

            def read1(self, _amount):
                value = next(self.values)
                if isinstance(value, BaseException):
                    raise value
                return value

        opener = Mock()
        opener.open.side_effect = [
            Response([b"head", http.client.IncompleteRead(b"tail")]),
            Response([b"valid", b""]),
        ]
        events = []
        with patch("msp_bonds_bond_search.discovery.build_opener", return_value=opener):
            with patch("msp_bonds_bond_search.discovery._rate_limit"):
                with patch("msp_bonds_bond_search.discovery.time.sleep"):
                    payload = safe_get(TEST_URL, max_attempts=2, observer=events.append)

        self.assertEqual(payload, b"valid")
        self.assertEqual(events[0].reason_code, "INCOMPLETE_RESPONSE")
        self.assertEqual(events[0].response_bytes, len(b"headtail"))
        self.assertEqual(events[0].url, TEST_URL)
        self.assertEqual(events[1].response_bytes, len(b"valid"))

    def test_smartlab_slow_near_complete_body_uses_bounded_read_timeout_and_stops_at_content_length(self):
        from types import SimpleNamespace

        url = "https://smart-lab.ru/q/bonds/"
        body = b"abcdefghij"

        class FakeSocket:
            def __init__(self):
                self.timeouts = []

            def settimeout(self, value):
                self.timeouts.append(value)

        class Response:
            status = 200

            def __init__(self):
                self.headers = {
                    "Content-Length": str(len(body)),
                    "Content-Encoding": "identity",
                }
                self.socket = FakeSocket()
                self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=self.socket))
                self.values = iter((body[:-1], body[-1:], TimeoutError("must not read past Content-Length")))
                self.read_sizes = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return url

            def read1(self, amount):
                self.read_sizes.append(amount)
                value = next(self.values)
                if len(self.read_sizes) == 2:
                    time.sleep(0.01)
                if isinstance(value, BaseException):
                    raise value
                return value

        response = Response()
        opener = Mock()
        opener.open.return_value = response
        events = []
        with patch("msp_bonds_bond_search.discovery.build_opener", return_value=opener):
            with patch("msp_bonds_bond_search.discovery._rate_limit"):
                payload = safe_get(url, read_timeout=30, observer=events.append)

        self.assertEqual(payload, body)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)
        self.assertTrue(response.socket.timeouts)
        self.assertTrue(all(0 < value <= 30 for value in response.socket.timeouts))
        self.assertEqual(response.read_sizes, [len(body), 1])
        self.assertEqual(events[-1].content_length, len(body))
        self.assertEqual(events[-1].content_encoding, "identity")
        self.assertEqual(events[-1].body_framing, "content-length")
        self.assertTrue(events[-1].body_complete)

    def test_near_complete_body_ending_before_content_length_is_not_accepted(self):
        from types import SimpleNamespace

        url = "https://smart-lab.ru/q/bonds/"
        body = b"almost-complete"

        class FakeSocket:
            def settimeout(self, _value):
                pass

        class Response:
            status = 200
            headers = {"Content-Length": str(len(body) + 4)}
            fp = SimpleNamespace(raw=SimpleNamespace(_sock=FakeSocket()))

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return url

            def __init__(self):
                self.values = iter((body[:-1], body[-1:], b""))

            def read1(self, _amount):
                return next(self.values)

        opener = Mock()
        opener.open.return_value = Response()
        events = []
        with patch("msp_bonds_bond_search.discovery.build_opener", return_value=opener):
            with patch("msp_bonds_bond_search.discovery._rate_limit"):
                with self.assertRaises(SourceUnavailable) as caught:
                    safe_get(url, read_timeout=30, observer=events.append)

        self.assertEqual(caught.exception.code, "INCOMPLETE_RESPONSE")
        self.assertEqual(caught.exception.response_bytes, len(body))
        self.assertEqual(events[-1].content_length, len(body) + 4)
        self.assertFalse(events[-1].body_complete)

    def test_chunked_transfer_is_complete_only_after_reader_reports_end(self):
        from types import SimpleNamespace

        url = "https://smart-lab.ru/q/bonds/"

        class Response:
            status = 200
            headers = {"Transfer-Encoding": "chunked", "Content-Encoding": "identity"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return url

            def __init__(self):
                self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=Mock()))
                self.values = iter((b"<html>", b"body</html>", b""))

            def read1(self, _amount):
                return next(self.values)

        opener = Mock()
        opener.open.return_value = Response()
        events = []
        with patch("msp_bonds_bond_search.discovery.build_opener", return_value=opener):
            with patch("msp_bonds_bond_search.discovery._rate_limit"):
                payload = safe_get(url, read_timeout=30, observer=events.append)

        self.assertEqual(payload, b"<html>body</html>")
        self.assertEqual(events[-1].transfer_encoding, "chunked")
        self.assertEqual(events[-1].body_framing, "chunked")
        self.assertTrue(events[-1].body_complete)

    def test_chunked_body_without_terminating_chunk_is_incomplete(self):
        from types import SimpleNamespace

        url = "https://smart-lab.ru/q/bonds/"

        class Response:
            status = 200
            headers = {"Transfer-Encoding": "chunked"}

            def __init__(self):
                self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=Mock()))
                self.values = iter((b"<html>partial", http.client.IncompleteRead(b"")))

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return url

            def read1(self, _amount):
                value = next(self.values)
                if isinstance(value, BaseException):
                    raise value
                return value

        opener = Mock()
        opener.open.return_value = Response()
        events = []
        with patch("msp_bonds_bond_search.discovery.build_opener", return_value=opener):
            with patch("msp_bonds_bond_search.discovery._rate_limit"):
                with self.assertRaises(SourceUnavailable) as caught:
                    safe_get(url, read_timeout=30, observer=events.append)

        self.assertEqual(caught.exception.code, "INCOMPLETE_RESPONSE")
        self.assertEqual(caught.exception.response_bytes, len(b"<html>partial"))
        self.assertEqual(events[-1].body_framing, "chunked")
        self.assertFalse(events[-1].body_complete)

    def test_unsupported_content_encoding_is_reported_instead_of_parsed_as_html(self):
        url = "https://smart-lab.ru/q/bonds/"

        class Response:
            status = 200
            headers = {"Content-Encoding": "gzip"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return url

            def read1(self, _amount):
                raise AssertionError("unsupported encoded body must not be read")

        opener = Mock()
        opener.open.return_value = Response()
        events = []
        with patch("msp_bonds_bond_search.discovery.build_opener", return_value=opener):
            with patch("msp_bonds_bond_search.discovery._rate_limit"):
                with self.assertRaises(SourceUnavailable) as caught:
                    safe_get(url, read_timeout=30, observer=events.append)

        self.assertEqual(caught.exception.code, "UNSUPPORTED_CONTENT_ENCODING")
        self.assertEqual(events[-1].content_encoding, "gzip")
        self.assertFalse(events[-1].body_complete)

    def test_safe_get_preserves_hard_response_size_limit(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return TEST_URL

            def read1(self, _amount):
                return b"12345"

        opener = Mock()
        opener.open.return_value = Response()
        with patch("msp_bonds_bond_search.discovery.build_opener", return_value=opener):
            with patch("msp_bonds_bond_search.discovery._rate_limit"):
                with patch("msp_bonds_bond_search.discovery._MAX_BYTES", 4):
                    with self.assertRaises(SourceUnavailable) as caught:
                        safe_get(TEST_URL)

        self.assertEqual(caught.exception.code, "RESPONSE_TOO_LARGE")
        self.assertEqual(caught.exception.response_bytes, 5)
        self.assertEqual(caught.exception.source_url, TEST_URL)

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
