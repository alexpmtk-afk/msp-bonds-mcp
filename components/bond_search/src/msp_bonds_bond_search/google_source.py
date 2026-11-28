"""Read the two canonical policy tabs through the existing Google Workspace API.

No OAuth flow, token storage, spreadsheet ID, or Google write API is added here.
The caller may inject the already-authorized workspace.sheets_read function.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .catalog import PolicySourceSnapshot, SnapshotError

RULE_TAB = "логика поиска"
RESOURCE_TAB = "ресурсы поиска"
SHEET_RANGE = "A1:G100"
MAX_ROWS = 100
MAX_COLUMNS = 7


class WorkspaceReadError(RuntimeError):
    """Existing Google Workspace could not return a bounded policy snapshot."""


SheetsRead = Callable[[str, str], Mapping[str, Any]]


def _normalise_response(response: Mapping[str, Any], tab: str) -> list[list[str]]:
    if not isinstance(response, Mapping) or not isinstance(response.get("values"), list):
        # Do not include provider response bodies or credential diagnostics.
        raise WorkspaceReadError(f"Google Workspace read failed for tab {tab}")
    rows = response["values"]
    if len(rows) > MAX_ROWS:
        raise WorkspaceReadError(f"Google Workspace returned too many rows for tab {tab}")
    normalized: list[list[str]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) > MAX_COLUMNS:
            raise WorkspaceReadError(f"Unexpected row shape in tab {tab}")
        cells = [_cell_text(value) for value in row]
        cells.extend([""] * (MAX_COLUMNS - len(cells)))
        normalized.append(cells)
    return normalized


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _a1(tab: str) -> str:
    # Sheet names are fixed constants and quoted even without punctuation.
    escaped = tab.replace("'", "''")
    return f"'{escaped}'!{SHEET_RANGE}"


def load_policy_snapshot(
    spreadsheet_id: str,
    *,
    sheets_read: SheetsRead,
    fetched_at: datetime | None = None,
) -> PolicySourceSnapshot:
    """Load only the canonical rules/resources ranges, never portfolio positions."""
    if not isinstance(spreadsheet_id, str) or not spreadsheet_id.strip() or len(spreadsheet_id) > 200:
        raise WorkspaceReadError("A valid spreadsheet ID is required")
    if not callable(sheets_read):
        raise WorkspaceReadError("Existing Google Workspace reader is required")
    captured_at = fetched_at or datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        raise WorkspaceReadError("The policy snapshot time must include a timezone")

    try:
        rule_response = sheets_read(spreadsheet_id, _a1(RULE_TAB))
        resource_response = sheets_read(spreadsheet_id, _a1(RESOURCE_TAB))
        rule_rows = _normalise_response(rule_response, RULE_TAB)
        resource_rows = _normalise_response(resource_response, RESOURCE_TAB)
        return PolicySourceSnapshot.from_sheet_rows(
            rule_rows, resource_rows, fetched_at=captured_at,
        )
    except WorkspaceReadError:
        raise
    except Exception as exc:
        # Never surface OAuth payloads, tokens, request headers, or cell content.
        raise WorkspaceReadError(f"Canonical policy read failed ({type(exc).__name__})") from None


def existing_workspace_status() -> dict[str, object]:
    """Report whether the existing Windows Workspace credential is available.

    The function never returns the account alias, token, OAuth client path, or
    spreadsheet contents. It does not start authentication.
    """
    try:
        from msp_bonds_google.auth import token_state
        from msp_bonds_google.workspace import account_alias, sheets_read
        state = token_state(account_alias(), "drive")
        return {
            "workspace_component_available": callable(sheets_read),
            "configured": bool(state.get("configured")),
            "credential_backend": "Windows Credential Manager",
            "contains_token_values": False,
            "reason": "" if state.get("configured") else "EXISTING_DRIVE_CREDENTIAL_MISSING",
        }
    except Exception as exc:
        return {
            "workspace_component_available": False,
            "configured": False,
            "credential_backend": "Windows Credential Manager",
            "contains_token_values": False,
            "reason": f"WORKSPACE_STATUS_ERROR_{type(exc).__name__}",
        }


def load_from_existing_workspace(
    spreadsheet_id: str,
    *,
    fetched_at: datetime | None = None,
    sheets_read: SheetsRead | None = None,
) -> PolicySourceSnapshot:
    """Use msp_bonds_google.workspace.sheets_read; never starts a new OAuth flow."""
    reader = sheets_read
    if reader is None:
        status = existing_workspace_status()
        if not status["workspace_component_available"]:
            raise WorkspaceReadError(
                f"Existing Google Workspace component unavailable ({status['reason']})"
            )
        if not status["configured"]:
            raise WorkspaceReadError(
                "Existing Google Workspace Drive credential is missing from Windows Credential Manager"
            )
        try:
            from msp_bonds_google.workspace import sheets_read as existing_sheets_read
        except Exception as exc:
            raise WorkspaceReadError(
                f"Existing Google Workspace component unavailable ({type(exc).__name__})"
            ) from None
        reader = existing_sheets_read
    return load_policy_snapshot(
        spreadsheet_id, sheets_read=reader, fetched_at=fetched_at,
    )
