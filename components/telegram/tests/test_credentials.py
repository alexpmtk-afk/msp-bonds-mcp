import unittest

from msp_bonds_telegram.credentials import (
    KEYRING_SERVICE,
    KEYRING_USERNAME,
    WindowsCredentialProvider,
)
from msp_bonds_telegram.errors import (
    TelegramCredentialBackendError,
    TelegramCredentialUnavailable,
)


class FakeWindowsBackend:
    pass


class FakeKeyring:
    def __init__(self, backend, token="synthetic-token"):
        self.backend = backend
        self.token = token
        self.reads = []

    def get_keyring(self):
        return self.backend

    def get_password(self, service, username):
        self.reads.append((service, username))
        return self.token


class CredentialProviderTests(unittest.TestCase):
    def test_reads_only_expected_credential_entry_from_windows_backend(self):
        keyring = FakeKeyring(FakeWindowsBackend())
        provider = WindowsCredentialProvider(
            keyring_api=keyring,
            backend_type=FakeWindowsBackend,
            platform_name="nt",
        )
        self.assertEqual(provider.get_token(), "synthetic-token")
        self.assertEqual(keyring.reads, [(KEYRING_SERVICE, KEYRING_USERNAME)])

    def test_rejects_non_windows_or_custom_backend_before_reading_token(self):
        keyring = FakeKeyring(object())
        provider = WindowsCredentialProvider(
            keyring_api=keyring,
            backend_type=FakeWindowsBackend,
            platform_name="nt",
        )
        with self.assertRaises(TelegramCredentialBackendError):
            provider.get_token()
        self.assertEqual(keyring.reads, [])

    def test_rejects_non_windows_platform(self):
        provider = WindowsCredentialProvider(
            keyring_api=FakeKeyring(FakeWindowsBackend()),
            backend_type=FakeWindowsBackend,
            platform_name="posix",
        )
        with self.assertRaises(TelegramCredentialBackendError):
            provider.get_token()

    def test_missing_token_is_reported_without_secret_values(self):
        secret = "synthetic-token-value"
        provider = WindowsCredentialProvider(
            keyring_api=FakeKeyring(FakeWindowsBackend(), token=None),
            backend_type=FakeWindowsBackend,
            platform_name="nt",
        )
        with self.assertRaises(TelegramCredentialUnavailable) as caught:
            provider.get_token()
        self.assertNotIn(secret, str(caught.exception))

    def test_keyring_exception_is_sanitized(self):
        class RaisingKeyring(FakeKeyring):
            def get_password(self, service, username):
                raise RuntimeError("synthetic-token-value must not escape")

        provider = WindowsCredentialProvider(
            keyring_api=RaisingKeyring(FakeWindowsBackend()),
            backend_type=FakeWindowsBackend,
            platform_name="nt",
        )
        with self.assertRaises(TelegramCredentialUnavailable) as caught:
            provider.get_token()
        self.assertNotIn("synthetic-token-value", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
