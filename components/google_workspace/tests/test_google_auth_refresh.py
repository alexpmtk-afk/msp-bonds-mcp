"""Offline tests for Gmail refresh and shared OAuth loading.

All credentials, requests, and credential-store operations are synthetic mocks.
"""
from unittest import TestCase
from unittest.mock import Mock, patch

from google.auth.transport import requests as google_requests
from google.oauth2 import credentials as google_credentials

from msp_bonds_google import auth


GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SYNTHETIC_RECORD = {
    "token": "synthetic-expired-access-token",
    "refresh_token": "synthetic-refresh-token",
    "scopes": [GMAIL_SCOPE],
}


class GmailRefreshTests(TestCase):
    def make_credentials(self, *, expired=True, valid=False):
        credentials = Mock()
        credentials.expired = expired
        credentials.valid = valid
        credentials.refresh_token = "synthetic-refresh-token"
        credentials.scopes = [GMAIL_SCOPE]
        credentials.to_json.return_value = '{"token":"synthetic-refreshed-token"}'
        return credentials

    def test_expired_gmail_access_token_refreshes_and_is_saved(self):
        credentials = self.make_credentials()

        def refresh(_request):
            credentials.expired = False
            credentials.valid = True

        credentials.refresh.side_effect = refresh
        keyring = Mock()
        with (
            patch.object(auth, "_load_json", return_value=SYNTHETIC_RECORD),
            patch.object(auth, "_keyring", return_value=keyring),
            patch.object(google_credentials.Credentials, "from_authorized_user_info", return_value=credentials) as load,
            patch.object(google_requests, "Request", return_value=object()) as request,
        ):
            result = auth.load_credentials("synthetic-owner", "gmail")

        self.assertIs(result, credentials)
        load.assert_called_once_with(SYNTHETIC_RECORD, scopes=[GMAIL_SCOPE])
        request.assert_called_once_with()
        credentials.refresh.assert_called_once_with(request.return_value)
        keyring.set_password.assert_called_once_with(
            auth.APP_SERVICE,
            "synthetic-owner:gmail",
            credentials.to_json.return_value,
        )

    def test_valid_token_does_not_refresh_or_write(self):
        credentials = self.make_credentials(expired=False, valid=True)
        keyring = Mock()
        with (
            patch.object(auth, "_load_json", return_value=SYNTHETIC_RECORD),
            patch.object(auth, "_keyring", return_value=keyring),
            patch.object(google_credentials.Credentials, "from_authorized_user_info", return_value=credentials),
            patch.object(google_requests, "Request") as request,
        ):
            result = auth.load_credentials("synthetic-owner", "gmail")

        self.assertIs(result, credentials)
        credentials.refresh.assert_not_called()
        request.assert_not_called()
        keyring.set_password.assert_not_called()

    def test_refresh_error_is_sanitized_and_not_saved(self):
        credentials = self.make_credentials()
        credentials.refresh.side_effect = RuntimeError("synthetic-refresh-secret")
        keyring = Mock()
        with (
            patch.object(auth, "_load_json", return_value=SYNTHETIC_RECORD),
            patch.object(auth, "_keyring", return_value=keyring),
            patch.object(google_credentials.Credentials, "from_authorized_user_info", return_value=credentials),
        ):
            with self.assertRaises(auth.GoogleAuthError) as caught:
                auth.load_credentials("synthetic-owner", "gmail")

        self.assertNotIn("synthetic-refresh-secret", str(caught.exception))
        self.assertIn("RuntimeError", str(caught.exception))
        keyring.set_password.assert_not_called()

    def test_gmail_service_client_uses_shared_credentials_loader(self):
        credentials = object()
        with (
            patch.object(auth, "load_credentials", return_value=credentials) as load,
            patch("googleapiclient.discovery.build") as build,
        ):
            result = auth.service_client("synthetic-owner", "gmail")

        self.assertIs(result, build.return_value)
        load.assert_called_once_with("synthetic-owner", "gmail")
        build.assert_called_once_with("gmail", "v1", credentials=credentials, cache_discovery=False)


if __name__ == "__main__":
    import unittest

    unittest.main()
