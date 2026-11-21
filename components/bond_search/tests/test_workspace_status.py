"""Public-safe tests for the existing Workspace credential status probe."""
import sys
import types
import unittest
from unittest.mock import Mock, patch

from msp_bonds_bond_search.google_source import (
    WorkspaceReadError,
    existing_workspace_status,
    load_from_existing_workspace,
)


def _workspace_modules(configured: bool):
    package = types.ModuleType("msp_bonds_google")
    package.__path__ = []
    auth = types.ModuleType("msp_bonds_google.auth")
    auth.token_state = lambda account, service: {
        "service": service,
        "configured": configured,
        "contains_token_values": False,
    }
    workspace = types.ModuleType("msp_bonds_google.workspace")
    workspace.account_alias = lambda: "synthetic-alias"
    workspace.sheets_read = Mock(return_value={"values": []})
    return package, auth, workspace


class WorkspaceStatusTests(unittest.TestCase):
    def test_missing_existing_drive_credential_is_reported_without_values(self):
        package, auth, workspace = _workspace_modules(configured=False)
        with patch.dict(sys.modules, {
            "msp_bonds_google": package,
            "msp_bonds_google.auth": auth,
            "msp_bonds_google.workspace": workspace,
        }):
            status = existing_workspace_status()

        self.assertFalse(status["configured"])
        self.assertTrue(status["workspace_component_available"])
        self.assertFalse(status["contains_token_values"])
        self.assertEqual(status["reason"], "EXISTING_DRIVE_CREDENTIAL_MISSING")
        self.assertNotIn("synthetic-alias", repr(status))

    def test_reader_fails_closed_without_starting_auth_or_reading_sheet(self):
        package, auth, workspace = _workspace_modules(configured=False)
        with patch.dict(sys.modules, {
            "msp_bonds_google": package,
            "msp_bonds_google.auth": auth,
            "msp_bonds_google.workspace": workspace,
        }):
            with self.assertRaises(WorkspaceReadError) as caught:
                load_from_existing_workspace("synthetic-spreadsheet-id")

        self.assertIn("Windows Credential Manager", str(caught.exception))
        workspace.sheets_read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
