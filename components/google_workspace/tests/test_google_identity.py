"""Offline synthetic account-consistency checks."""
import unittest
from unittest.mock import patch, MagicMock

from msp_bonds_google import identity as gi
from msp_bonds_google.utils import InputError


class IdentityTests(unittest.TestCase):
    def _clients(self, drive_email, gmail_email):
        drive = MagicMock()
        gmail = MagicMock()
        drive.about.return_value.get.return_value.execute.return_value = {"user": {"emailAddress": drive_email}}
        gmail.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": gmail_email}
        return {"drive": drive, "gmail": gmail}

    def test_matching_identities_without_disclosing_emails(self):
        clients = self._clients("Owner@Example.test", "owner@example.test")
        with patch.object(gi, "service_client", side_effect=lambda alias, svc: clients[svc]):
            result = gi.connection_health()
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["same_account"])
        self.assertNotIn("example.test", str(result))

    def test_different_accounts_are_blocked(self):
        clients = self._clients("owner@example.test", "someone-else@example.test")
        with patch.object(gi, "service_client", side_effect=lambda alias, svc: clients[svc]):
            with self.assertRaises(InputError):
                gi.assert_same_account()

    def test_missing_identity_rejected(self):
        clients = self._clients("", "owner@example.test")
        with patch.object(gi, "service_client", side_effect=lambda alias, svc: clients[svc]):
            self.assertEqual(gi.connection_health()["status"], "FAIL")
