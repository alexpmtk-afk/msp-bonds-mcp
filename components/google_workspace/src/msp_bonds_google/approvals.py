"""Explicit local-human approvals for all remote Google writes.

MCP calls can prepare and execute a previously locally approved plan, but they
cannot approve one. The approval CLI works only on HOME, not over MCP.
"""
from __future__ import annotations

import json
from contextlib import closing
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .utils import InputError
from msp_bonds_mcp.core.paths import runtime_dir

ALLOWED_ACTIONS = {
    "drive_create_text", "drive_upload_staged", "drive_replace_text",
    "drive_move", "drive_trash", "docs_replace_all", "sheets_update_values",
    "gmail_add_labels",
}


def _db_path() -> Path:
    path = runtime_dir() / "approvals.sqlite3"
    path.parent.mkdir(exist_ok=True, parents=True)
    return path


def _connect():
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS plans (
        id TEXT PRIMARY KEY, action TEXT NOT NULL, params TEXT NOT NULL,
        status TEXT NOT NULL, created TEXT NOT NULL, expires TEXT NOT NULL)""")
    conn.commit()
    return conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check_payload(action: str, payload: dict):
    if action not in ALLOWED_ACTIONS:
        raise InputError("Unknown or forbidden Google write action")
    if not isinstance(payload, dict):
        raise InputError("Write parameters must be a JSON object")
    requirements = {
        "drive_create_text": ("parent_id", "name", "text"),
        "drive_upload_staged": ("parent_id", "staged_name", "name", "expected_sha256"),
        "drive_replace_text": ("file_id", "text", "expected_version"),
        "drive_move": ("file_id", "new_parent_id", "expected_old_parent_id", "expected_version"),
        "drive_trash": ("file_id", "expected_version"),
        "docs_replace_all": ("document_id", "find", "replacement", "expected_revision_id"),
        "sheets_update_values": ("spreadsheet_id", "range", "rows", "expected_values"),
        "gmail_add_labels": ("message_id", "add_label_ids", "remove_label_ids"),
    }
    for key in requirements[action]:
        if key not in payload:
            raise InputError(f"Missing required write parameter: {key}")
    if any(key not in set(requirements[action]) for key in payload):
        raise InputError("Unexpected write parameter")
    if len(json.dumps(payload, ensure_ascii=False)) > 150_000:
        raise InputError("Write plan too large")
    for key in ("file_id", "parent_id", "new_parent_id", "expected_old_parent_id",
                "document_id", "spreadsheet_id", "message_id", "name", "staged_name"):
        if key in payload and (not isinstance(payload[key], str) or not payload[key].strip()):
            raise InputError(f"Nonempty string required: {key}")
    if action == "docs_replace_all" and not payload["find"]:
        raise InputError("Empty find-text would be unsafe")
    if action == "sheets_update_values" and (not isinstance(payload["rows"], list) or len(payload["rows"]) > 500):
        raise InputError("Rows must be a short list")


def propose(action: str, params_json: str) -> dict:
    try:
        params = json.loads(params_json)
    except Exception as exc:
        raise InputError("Invalid JSON parameters") from exc
    _check_payload(action, params)
    # No contact with Google. Store plan locally; tokens never in database.
    plan_id = uuid.uuid4().hex
    now = _now()
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT INTO plans VALUES (?, ?, ?, ?, ?, ?)",
                     (plan_id, action, json.dumps(params, ensure_ascii=False), "PENDING",
                      now.isoformat(), (now + timedelta(minutes=30)).isoformat()))
    return {"plan_id": plan_id, "action": action, "status": "PENDING_LOCAL_APPROVAL",
            "instruction": "Owner must approve this exact plan locally using the Python CLI; MCP cannot approve"}


def get_plan(plan_id: str, include_params: bool = True) -> dict:
    with closing(_connect()) as conn, conn:
        row = conn.execute("SELECT action,params,status,created,expires FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not row:
        raise InputError("Unknown plan ID")
    if _now() > datetime.fromisoformat(row[4]):
        raise InputError("Approval has expired")
    plan = {"plan_id": plan_id, "action": row[0], "status": row[2], "expires": row[4]}
    if include_params:
        plan["params"] = json.loads(row[1])
    return plan


def approve_interactively(plan_id: str):
    plan = get_plan(plan_id)
    if plan["status"] != "PENDING":
        raise InputError("Plan no longer pending")
    print("LOCAL APPROVAL for:", plan["action"])
    print(json.dumps(plan["params"], ensure_ascii=False, indent=2))
    print("No Google change will happen from this CLI. You only approve one future execution.")
    response = input("Type the exact word APPROVE: ")
    if response != "APPROVE":
        return {"approved": False}
    with closing(_connect()) as conn, conn:
        cur = conn.execute("UPDATE plans SET status='APPROVED' WHERE id=? AND status='PENDING'", (plan_id,))
        if cur.rowcount != 1:
            raise InputError("Concurrent update; no approval")
    return {"approved": True, "plan_id": plan_id}


def consume_approved(plan_id: str) -> dict:
    plan = get_plan(plan_id)
    if plan["status"] != "APPROVED":
        raise InputError("Plan has not been locally approved")
    with closing(_connect()) as conn, conn:
        # One-time use; mark consumed before any external action.
        cur = conn.execute("UPDATE plans SET status='CONSUMED' WHERE id=? AND status='APPROVED'", (plan_id,))
        if cur.rowcount != 1:
            raise InputError("Approval already used")
    return plan
