"""Mocked curl transport and page-cache tests; these tests make no network calls."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from msp_bonds_bond_search.discovery import FetchDiagnostic, SourceUnavailable
from msp_bonds_bond_search.smartlab_curl import (
    SmartLabPageCache,
    _CurlResponse,
    _run_curl,
    fetch_smartlab_html,
)


PAGE_URL = "https://smart-lab.ru/q/bonds/"
PAGE_TWO_URL = "https://smart-lab.ru/q/bonds/order_by_val_to_day/desc/page2/"


class _FakeProcess:
    def __init__(self, args, output: bytes, headers: bytes, return_code: int = 0):
        self.args = args
        self.returncode = return_code
        self.stdout = io.BytesIO(output)
        header_path = Path(args[args.index("--dump-header") + 1])
        header_path.write_bytes(headers)
        self.killed = False

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class SmartLabCurlTests(unittest.TestCase):
    def test_curl_uses_argument_list_compression_https_and_fixed_host(self):
        body = b"<html>screen</html>"
        output = body + b"\n__MSP_SMARTLAB_META__200\t19\n"
        headers = (
            b"HTTP/1.1 200 OK\r\nContent-Length: 19\r\n"
            b"Content-Encoding: identity\r\nETag: \"v1\"\r\n\r\n"
        )
        processes = []

        def make_process(args, **kwargs):
            process = _FakeProcess(args, output, headers)
            processes.append((process, kwargs))
            return process

        with patch("msp_bonds_bond_search.smartlab_curl.subprocess.Popen", side_effect=make_process):
            with patch("msp_bonds_bond_search.discovery._rate_limit"):
                response = _run_curl(PAGE_URL, attempt=1, conditional_headers=(), observer=None)

        process, kwargs = processes[0]
        args = process.args
        self.assertEqual(response.body, body)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.wire_bytes, 19)
        self.assertIn("--compressed", args)
        self.assertIn("--max-time", args)
        self.assertIn("--max-filesize", args)
        self.assertEqual(args[1], "--disable")
        self.assertEqual(args[args.index("--url") + 1], PAGE_URL)
        self.assertIsInstance(args, list)
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["stderr"], -3)  # subprocess.DEVNULL

    def test_decompressed_output_limit_kills_oversized_transfer(self):
        output = b"x" * 400
        headers = b"HTTP/1.1 200 OK\r\n\r\n"
        processes = []

        def make_process(args, **kwargs):
            process = _FakeProcess(args, output, headers)
            processes.append(process)
            return process

        with patch("msp_bonds_bond_search.smartlab_curl._MAX_HTML_BYTES", 100):
            with patch("msp_bonds_bond_search.smartlab_curl.subprocess.Popen", side_effect=make_process):
                with patch("msp_bonds_bond_search.discovery._rate_limit"):
                    with self.assertRaises(SourceUnavailable) as caught:
                        _run_curl(PAGE_URL, attempt=1, conditional_headers=(), observer=None)

        self.assertEqual(caught.exception.code, "RESPONSE_TOO_LARGE")
        self.assertTrue(processes[0].killed)

    def test_non_smartlab_url_is_rejected_before_subprocess(self):
        with patch("msp_bonds_bond_search.smartlab_curl.subprocess.Popen") as popen:
            with self.assertRaises(SourceUnavailable) as caught:
                _run_curl("https://example.com/q/bonds/", attempt=1, conditional_headers=(), observer=None)
        self.assertEqual(caught.exception.code, "URL_NOT_ALLOWED")
        popen.assert_not_called()

    def test_fresh_cache_fallback_requires_valid_hash_sort_and_age(self):
        now = datetime.now(timezone.utc)
        body = f'<a href="{PAGE_TWO_URL}">2</a>'.encode()
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = SmartLabPageCache(temp_dir, max_fallback_age_seconds=300)
            cache.save(PAGE_URL, body, {"etag": '"v1"'}, now=now)
            cached = cache.load(PAGE_URL)
            self.assertIsNotNone(cached)
            self.assertEqual(cached.sort_signature, "val_to_day:desc")

            attempts = []

            def fail_temporarily(url, *, attempt, conditional_headers, observer=None):
                attempts.append((attempt, conditional_headers))
                raise SourceUnavailable(
                    "temporary curl timeout", code="CURL_TIMEOUT", retryable=True,
                    attempts=attempt, source_url=url,
                )

            diagnostics: list[FetchDiagnostic] = []
            with patch("msp_bonds_bond_search.smartlab_curl._run_curl", side_effect=fail_temporarily):
                with patch("msp_bonds_bond_search.smartlab_curl.time.sleep"):
                    result = fetch_smartlab_html(PAGE_URL, observer=diagnostics.append, cache=cache)

            self.assertEqual(result, body)
            self.assertEqual([item[0] for item in attempts], [1, 2])
            self.assertIn('If-None-Match: "v1"', attempts[0][1])
            self.assertEqual(diagnostics[-1].cache_status, "FRESH_FALLBACK")
            self.assertEqual(diagnostics[-1].content_fetched_at, now.isoformat())

            body_path, metadata_path = cache._paths(PAGE_URL)
            metadata = __import__("json").loads(metadata_path.read_text(encoding="utf-8"))
            metadata["sort_signature"] = "other:asc"
            metadata_path.write_text(__import__("json").dumps(metadata), encoding="utf-8")
            self.assertIsNone(cache.load(PAGE_URL))
            body_path.unlink(missing_ok=True)

    def test_old_cache_is_not_used_after_temporary_failures(self):
        now = datetime.now(timezone.utc)
        body = f'<a href="{PAGE_TWO_URL}">2</a>'.encode()
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = SmartLabPageCache(temp_dir, max_fallback_age_seconds=60)
            cache.save(PAGE_URL, body, {}, now=now - timedelta(minutes=10))

            def fail_temporarily(url, *, attempt, conditional_headers, observer=None):
                raise SourceUnavailable(
                    "temporary curl timeout", code="CURL_TIMEOUT", retryable=True,
                    attempts=attempt, source_url=url,
                )

            with patch("msp_bonds_bond_search.smartlab_curl._run_curl", side_effect=fail_temporarily):
                with patch("msp_bonds_bond_search.smartlab_curl.time.sleep"):
                    with self.assertRaises(SourceUnavailable) as caught:
                        fetch_smartlab_html(PAGE_URL, cache=cache)
        self.assertEqual(caught.exception.code, "CURL_TIMEOUT")

    def test_http_304_reuses_cache_only_after_validator_request(self):
        now = datetime.now(timezone.utc)
        body = f'<a href="{PAGE_TWO_URL}">2</a>'.encode()
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = SmartLabPageCache(temp_dir)
            cache.save(PAGE_URL, body, {"etag": '"v2"'}, now=now)
            seen = []

            def not_modified(url, *, attempt, conditional_headers, observer=None):
                seen.extend(conditional_headers)
                return _CurlResponse(b"", 304, 0, {}, 0.01, 0)

            diagnostics = []
            with patch("msp_bonds_bond_search.smartlab_curl._run_curl", side_effect=not_modified):
                result = fetch_smartlab_html(PAGE_URL, observer=diagnostics.append, cache=cache)

            self.assertEqual(result, body)
            self.assertIn('If-None-Match: "v2"', seen)
            self.assertEqual(diagnostics[-1].cache_status, "REVALIDATED")
            self.assertEqual(cache.load(PAGE_URL).body, body)


if __name__ == "__main__":
    unittest.main()
