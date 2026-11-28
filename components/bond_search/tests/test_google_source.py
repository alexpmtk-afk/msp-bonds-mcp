import unittest
from datetime import datetime, timezone

from msp_bonds_bond_search.google_source import (
    WorkspaceReadError,
    load_from_existing_workspace,
    load_policy_snapshot,
)
from policy_fixtures import RULES, SOURCES

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


class GoogleSourceTests(unittest.TestCase):
    def test_reads_only_policy_and_resource_tabs_with_bounded_ranges(self):
        calls = []

        def reader(spreadsheet_id, a1_range):
            calls.append((spreadsheet_id, a1_range))
            if a1_range == "'логика поиска'!A1:G100":
                return {"range": a1_range, "values": RULES}
            if a1_range == "'ресурсы поиска'!A1:G100":
                return {"range": a1_range, "values": SOURCES}
            self.fail("unexpected range")

        snapshot = load_policy_snapshot(
            "synthetic-spreadsheet-id",
            sheets_read=reader,
            fetched_at=NOW,
        )
        self.assertEqual(
            calls,
            [
                ("synthetic-spreadsheet-id", "'логика поиска'!A1:G100"),
                ("synthetic-spreadsheet-id", "'ресурсы поиска'!A1:G100"),
            ],
        )
        self.assertEqual(len(snapshot.rules), len(RULES) - 1)
        self.assertEqual(len(snapshot.resources), len(SOURCES) - 1)

    def test_existing_workspace_reader_can_be_injected_without_auth_flow(self):
        calls = []

        def reader(spreadsheet_id, a1_range):
            calls.append(a1_range)
            return {"values": RULES if "логика поиска" in a1_range else SOURCES}

        snapshot = load_from_existing_workspace(
            "synthetic-spreadsheet-id",
            fetched_at=NOW,
            sheets_read=reader,
        )
        self.assertEqual(len(snapshot.resources), len(SOURCES) - 1)
        self.assertEqual(len(calls), 2)

    def test_provider_error_does_not_leak_provider_text(self):
        secret_like = "credential-placeholder-must-not-appear"

        def reader(_spreadsheet_id, _a1_range):
            raise RuntimeError(secret_like)

        with self.assertRaises(WorkspaceReadError) as ctx:
            load_policy_snapshot(
                "synthetic-spreadsheet-id",
                sheets_read=reader,
                fetched_at=NOW,
            )
        self.assertNotIn(secret_like, str(ctx.exception))

    def test_invalid_response_shape_fails_closed(self):
        with self.assertRaises(WorkspaceReadError):
            load_policy_snapshot(
                "synthetic-spreadsheet-id",
                sheets_read=lambda *_: {"values": "not rows"},
                fetched_at=NOW,
            )

    def test_invalid_spreadsheet_id_rejected_before_reader_call(self):
        def reader(*_):
            self.fail("reader should not be called")

        with self.assertRaises(WorkspaceReadError):
            load_policy_snapshot(" ", sheets_read=reader, fetched_at=NOW)


if __name__ == "__main__":
    unittest.main()


class ExistingWorkspaceStatusTests(unittest.TestCase):
    def test_missing_credential_is_reported_without_exposing_alias_or_tokens(self):
        import sys
        import types
        from unittest.mock import patch
        from msp_bonds_bond_search.google_source import existing_workspace_status

        package = types.ModuleType("msp_bonds_google")
        package.__path__ = []
        auth = types.ModuleType("msp_bonds_google.auth")
        auth.token_state = lambda account, service: {
            "service": service, "configured": False, "contains_token_values": False,
        }
        workspace = types.ModuleType("msp_bonds_google.workspace")
        workspace.account_alias = lambda: "private-test-alias"
        workspace.sheets_read = lambda spreadsheet_id, a1_range: {"values": []}
        with patch.dict(sys.modules, {
            "msp_bonds_google": package,
            "msp_bonds_google.auth": auth,
            "msp_bonds_google.workspace": workspace,
        }):
            status = existing_workspace_status()

        self.assertFalse(status["configured"])
        self.assertTrue(status["workspace_component_available"])
        self.assertFalse(status["contains_token_values"])
        self.assertNotIn("private-test-alias", repr(status))
        self.assertEqual(status["reason"], "EXISTING_DRIVE_CREDENTIAL_MISSING")
