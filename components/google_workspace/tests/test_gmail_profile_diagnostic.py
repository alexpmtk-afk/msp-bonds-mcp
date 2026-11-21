"""Synthetic offline tests for the Gmail profile diagnostic; no real API calls."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from msp_bonds_google.gmail_profile_diagnostic import (
    ERROR,
    EXIT_CREDENTIAL_RECORD_MISSING,
    EXIT_GMAIL_HTTP_403,
    EXIT_NETWORK_TIMEOUT,
    EXIT_SCOPE_MISMATCH,
    EXIT_TOKEN_EXPIRED,
    GMAIL_READONLY_SCOPE,
    PASS,
    PROFILE_URL,
    REQUEST_TIMEOUT_SECONDS,
    _NoRedirectHandler,
    _usable_access_token,
    gmail_profile_status,
    main,
)


SYNTHETIC_TOKEN = "synthetic-test-token-not-a-real-credential"


class GmailProfileDiagnosticTests(unittest.TestCase):
    def _opener(self, *, error=None, status=200):
        opener = Mock()
        response = MagicMock()
        response.getcode.return_value = status
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        if error is not None:
            opener.open.side_effect = error
        else:
            opener.open.return_value = response
        factory = Mock(return_value=opener)
        return factory, opener, response

    def test_http_200_returns_only_pass_and_does_not_read_profile(self):
        factory, opener, response = self._opener()
        response.read.side_effect = AssertionError("response body must stay unread")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = gmail_profile_status(
                SYNTHETIC_TOKEN,
                credentials_valid=True,
                credentials_expired=False,
                opener_factory=factory,
            )
        self.assertEqual(result, PASS)
        self.assertEqual(str(result), "PASS")
        self.assertNotIn("synthetic", str(result))
        response.read.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(opener.open.call_count, 1)
        request = opener.open.call_args.args[0]
        kwargs = opener.open.call_args.kwargs
        self.assertEqual(request.full_url, PROFILE_URL)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {SYNTHETIC_TOKEN}")
        self.assertEqual(kwargs["timeout"], REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(len(factory.call_args.args), 1)
        self.assertIsInstance(factory.call_args.args[0], _NoRedirectHandler)

    def test_401_and_access_not_configured_403_return_error_without_reading_body(self):
        for status, message in ((401, "Unauthorized"), (403, "accessNotConfigured")):
            with self.subTest(status=status):
                body = Mock()
                error = HTTPError(PROFILE_URL, status, message, hdrs=None, fp=body)
                factory, opener, _ = self._opener(error=error)
                result = gmail_profile_status(
                    SYNTHETIC_TOKEN,
                    credentials_valid=True,
                    credentials_expired=False,
                    opener_factory=factory,
                )
                self.assertEqual(result, ERROR)
                self.assertEqual(opener.open.call_count, 1)
                body.read.assert_not_called()
                self.assertNotIn(message, str(result))

    def test_missing_invalid_or_expired_test_credentials_fail_before_network(self):
        cases = (
            (None, True, False),
            ("", True, False),
            (SYNTHETIC_TOKEN, False, False),
            (SYNTHETIC_TOKEN, True, True),
        )
        for token, valid, expired in cases:
            with self.subTest(token_missing=not token, valid=valid, expired=expired):
                factory = Mock()
                result = gmail_profile_status(
                    token,
                    credentials_valid=valid,
                    credentials_expired=expired,
                    opener_factory=factory,
                )
                self.assertEqual(result, ERROR)
                factory.assert_not_called()

    def test_network_error_and_timeout_return_error_without_retry(self):
        for error in (URLError("synthetic network failure"), TimeoutError("synthetic timeout")):
            with self.subTest(error_type=type(error).__name__):
                factory, opener, _ = self._opener(error=error)
                result = gmail_profile_status(
                    SYNTHETIC_TOKEN,
                    credentials_valid=True,
                    credentials_expired=False,
                    opener_factory=factory,
                )
                self.assertEqual(result, ERROR)
                self.assertEqual(opener.open.call_count, 1)
                self.assertNotIn(SYNTHETIC_TOKEN, str(result))

    def test_redirect_is_rejected_and_not_followed(self):
        factory, opener, _ = self._opener(
            error=HTTPError(PROFILE_URL, 302, "Found", {"Location": "https://example.invalid/"}, io.BytesIO())
        )
        result = gmail_profile_status(
            SYNTHETIC_TOKEN,
            credentials_valid=True,
            credentials_expired=False,
            opener_factory=factory,
        )
        self.assertEqual(result, ERROR)
        self.assertEqual(opener.open.call_count, 1)
        handler = factory.call_args.args[0]
        self.assertIsInstance(handler, _NoRedirectHandler)
        request = Request(PROFILE_URL)
        self.assertIsNone(
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {"Location": "https://example.invalid/"},
                "https://example.invalid/",
            )
        )

    def test_diagnostic_never_loads_or_writes_oauth_credentials(self):
        factory, _, _ = self._opener()
        with (
            patch("msp_bonds_google.auth.load_credentials") as load_credentials,
            patch("msp_bonds_google.auth._keyring") as keyring,
        ):
            result = gmail_profile_status(
                SYNTHETIC_TOKEN,
                credentials_valid=True,
                credentials_expired=False,
                opener_factory=factory,
            )
        self.assertEqual(result, PASS)
        load_credentials.assert_not_called()
        keyring.assert_not_called()
        self.assertEqual(factory.return_value.open.call_count, 1)

    def test_module_entry_point_fails_closed_without_network_or_credentials(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            patch("msp_bonds_google.gmail_profile_diagnostic.urllib.request.build_opener") as build_opener,
            patch("msp_bonds_google.gmail_profile_diagnostic._load_saved_gmail_credentials", return_value=None),
            patch("msp_bonds_google.auth.load_credentials") as load_credentials,
            patch("msp_bonds_google.auth._keyring") as keyring,
        ):
            exit_code = main()

        self.assertEqual(exit_code, EXIT_CREDENTIAL_RECORD_MISSING)
        self.assertEqual(stdout.getvalue(), "ERROR\n")
        self.assertEqual(stderr.getvalue(), "")
        build_opener.assert_not_called()
        load_credentials.assert_not_called()
        keyring.assert_not_called()

    def test_saved_token_requires_valid_expiry_and_exact_readonly_scope(self):
        from datetime import datetime, timedelta, timezone

        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        valid = {
            "token": SYNTHETIC_TOKEN,
            "scopes": [GMAIL_READONLY_SCOPE],
            "expiry": (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
        }
        self.assertEqual(_usable_access_token(valid, now=now), SYNTHETIC_TOKEN)

        invalid_cases = (
            None,
            {},
            {**valid, "scopes": [GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.modify"]},
            {**valid, "expiry": (now - timedelta(seconds=1)).isoformat()},
            {**valid, "expiry": "not-a-date"},
            {**valid, "expiry": "2026-11-18T00:30:00"},
            {**valid, "token": ""},
        )
        for credentials in invalid_cases:
            with self.subTest(credentials_type=type(credentials).__name__):
                self.assertIsNone(_usable_access_token(credentials, now=now))

    def test_entry_point_reads_only_synthetic_record_and_makes_one_mocked_request(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        credentials = {
            "token": SYNTHETIC_TOKEN,
            "scopes": [GMAIL_READONLY_SCOPE],
            "expiry": (now + timedelta(minutes=10)).isoformat(),
        }
        factory, opener, response = self._opener()
        response.read.side_effect = AssertionError("profile response body must stay unread")
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            patch("msp_bonds_google.auth._load_json", return_value=credentials) as load_json,
            patch("msp_bonds_google.auth._keyring") as keyring,
            patch("msp_bonds_google.auth.load_credentials") as load_credentials,
            patch("msp_bonds_google.workspace.account_alias", return_value="synthetic-alias"),
            patch("msp_bonds_google.gmail_profile_diagnostic.urllib.request.build_opener", factory),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "PASS\n")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(SYNTHETIC_TOKEN, stdout.getvalue() + stderr.getvalue())
        load_json.assert_called_once_with("synthetic-alias", "gmail")
        keyring.assert_not_called()
        load_credentials.assert_not_called()
        self.assertEqual(opener.open.call_count, 1)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, PROFILE_URL)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {SYNTHETIC_TOKEN}")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], REQUEST_TIMEOUT_SECONDS)
        response.read.assert_not_called()

    def test_entry_point_rejects_expired_record_without_network_or_refresh(self):
        credentials = {
            "token": SYNTHETIC_TOKEN,
            "scopes": [GMAIL_READONLY_SCOPE],
            "expiry": "2000-01-01T00:00:00Z",
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            patch("msp_bonds_google.auth._load_json", return_value=credentials),
            patch("msp_bonds_google.auth._keyring") as keyring,
            patch("msp_bonds_google.auth.load_credentials") as load_credentials,
            patch("msp_bonds_google.workspace.account_alias", return_value="synthetic-alias"),
            patch("msp_bonds_google.gmail_profile_diagnostic.urllib.request.build_opener") as build_opener,
        ):
            exit_code = main()

        self.assertEqual(exit_code, EXIT_TOKEN_EXPIRED)
        self.assertEqual(stdout.getvalue(), "ERROR\n")
        self.assertEqual(stderr.getvalue(), "")
        keyring.assert_not_called()
        load_credentials.assert_not_called()
        build_opener.assert_not_called()

    def test_entry_point_reports_safe_http_failure_in_exit_code_only(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        credentials = {
            "token": SYNTHETIC_TOKEN,
            "scopes": [GMAIL_READONLY_SCOPE],
            "expiry": (now + timedelta(minutes=10)).isoformat(),
        }
        response_body = Mock()
        factory, opener, _ = self._opener(
            error=HTTPError(PROFILE_URL, 403, "synthetic forbidden", hdrs=None, fp=response_body)
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            patch("msp_bonds_google.gmail_profile_diagnostic._load_saved_gmail_credentials", return_value=credentials),
            patch("msp_bonds_google.gmail_profile_diagnostic.urllib.request.build_opener", factory),
        ):
            exit_code = main()

        self.assertEqual(stdout.getvalue(), "ERROR\n")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(exit_code, EXIT_GMAIL_HTTP_403)
        self.assertEqual(opener.open.call_count, 1)
        response_body.read.assert_not_called()


if __name__ == "__main__":
    unittest.main()

