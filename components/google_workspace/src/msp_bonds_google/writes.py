"""Explicitly approved Google mutations, never invoked by startup or read tools."""
from __future__ import annotations
import io
import os
from pathlib import Path

from .approvals import consume_approved
from .identity import assert_same_account
from .auth import service_client, workspace_client
from .utils import InputError, safe_filename, sha256_bytes
from .workspace import (MAX_BYTES, account_alias, drive_metadata, staging_dir,
                               _drive, _doc, _sheets)


def _match_version(file_id: str, expected_version: str) -> dict:
    meta = drive_metadata(file_id)
    if str(meta.get("version", "")) != str(expected_version):
        raise InputError("Drive file version has changed; prepare a new plan")
    if meta.get("trashed"):
        raise InputError("File already in trash")
    return meta


def _ensure_unique(parent_id: str, name: str):
    from .utils import drive_q_literal
    name = safe_filename(name)
    q = f"'{drive_q_literal(parent_id)}' in parents and name = '{drive_q_literal(name)}' and trashed = false"
    r = _drive().files().list(q=q, fields="files(id,name),nextPageToken",pageSize=10).execute()
    if r.get("files"):
        raise InputError("A file with this name already exists in destination; not uploading")


def _readback(file_id: str, content: bytes) -> dict:
    from googleapiclient.http import MediaIoBaseDownload
    req = _drive().files().get_media(fileId=file_id)
    output = io.BytesIO()
    dl = MediaIoBaseDownload(output, req, chunksize=1024 * 1024)
    done = False
    while not done:
        _, done = dl.next_chunk()
        if output.tell() > MAX_BYTES:
            raise InputError("Remote readback exceeds safe limit")
    identical = sha256_bytes(output.getvalue()) == sha256_bytes(content)
    return {"sha256_verified": identical, "sha256": sha256_bytes(content)}


def _upload(data: bytes, name: str, parent_id: str, mimetype: str) -> dict:
    if not data or len(data) > MAX_BYTES:
        raise InputError("Upload must be nonempty and within 25 MiB")
    name = safe_filename(name)
    _ensure_unique(parent_id, name)
    from googleapiclient.http import MediaIoBaseUpload
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype, resumable=True)
    result = _drive().files().create(body={"name": name, "parents": [parent_id]},
                                     media_body=media,fields="id,name,size,mimeType",supportsAllDrives=True).execute()
    verify = _readback(result["id"], data)
    return {"file_id": result["id"], "name": name, **verify,
            "status": "VERIFIED" if verify["sha256_verified"] else "READBACK_MISMATCH_NEEDS_REVIEW"}


def _mime_for_name(name: str) -> str:
    if name.lower().endswith(".pdf"):
        return "application/pdf"
    if name.lower().endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if name.lower().endswith(".txt"):
        return "text/plain"
    return "application/octet-stream"


def execute(plan_id: str) -> dict:
    # Network mutation is impossible unless owner approved locally and enabled writes.
    if os.environ.get("MSP_GOOGLE_WRITES_ENABLED", "0") != "1":
        raise InputError("Google writes are disabled in local process configuration")
    plan = consume_approved(plan_id)
    action, p = plan["action"], plan["params"]

    if action == "drive_create_text":
        return _upload(p["text"].encode("utf-8"), p["name"], p["parent_id"], "text/plain")

    if action == "drive_upload_staged":
        if safe_filename(p["staged_name"]) != p["staged_name"]:
            raise InputError("Invalid staged name")
        path = staging_dir() / p["staged_name"]
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_BYTES:
            raise InputError("Staged file missing or too large")
        content = path.read_bytes()
        if sha256_bytes(content) != p["expected_sha256"]:
            raise InputError("Staged attachment changed after approval; no upload")
        # Prevent transferring email attachments into a different Google account.
        assert_same_account()
        result = _upload(content, p["name"], p["parent_id"], _mime_for_name(p["name"]))
        if result.get("status") == "VERIFIED":
            try:
                from msp_bonds_vtb_reports.vtb_registry_refresh import refresh_vtb_registry
                result["vtb_registry"] = refresh_vtb_registry(
                    target_parent_id=p["parent_id"], expected_name=p["name"],
                    expected_file_id=result["file_id"], expected_sha256=result["sha256"],
                )
            except Exception as exc:
                # Upload is already complete; never mask success with indexing failure.
                result["vtb_registry"] = {"status": "ERROR", "error_type": type(exc).__name__}
        return result

    if action == "drive_replace_text":
        meta = _match_version(p["file_id"], p["expected_version"])
        if not meta.get("mimeType", "").startswith("text/"):
            raise InputError("Only text/* binary files can be replaced with this action")
        data = p["text"].encode("utf-8")
        if len(data) > MAX_BYTES:
            raise InputError("Text too long")
        from googleapiclient.http import MediaIoBaseUpload
        result = _drive().files().update(fileId=p["file_id"],media_body=MediaIoBaseUpload(io.BytesIO(data),mimetype="text/plain",resumable=True),fields="id,version",supportsAllDrives=True).execute()
        return {"file_id": result["id"], **_readback(result["id"], data)}

    if action == "drive_move":
        meta = _match_version(p["file_id"], p["expected_version"])
        if p["expected_old_parent_id"] not in meta.get("parents", []):
            raise InputError("Original parent has changed")
        r = _drive().files().update(fileId=p["file_id"], addParents=p["new_parent_id"],
                                     removeParents=p["expected_old_parent_id"],
                                     fields="id,parents,version",supportsAllDrives=True).execute()
        return {"file_id": r["id"], "parents": r.get("parents", [])}

    if action == "drive_trash":
        _match_version(p["file_id"], p["expected_version"])
        r = _drive().files().update(fileId=p["file_id"],body={"trashed": True},fields="id,trashed",supportsAllDrives=True).execute()
        return {"file_id": r["id"], "trashed": r.get("trashed")}

    if action == "docs_replace_all":
        doc = _doc().documents().get(documentId=p["document_id"],fields="documentId,revisionId").execute()
        if doc.get("revisionId") != p["expected_revision_id"]:
            raise InputError("Document revision has changed; do not overwrite")
        r = _doc().documents().batchUpdate(documentId=p["document_id"],body={
            "requests": [{"replaceAllText": {"containsText": {"text": p["find"], "matchCase": True},
                                              "replaceText": p["replacement"]}}],
            "writeControl": {"requiredRevisionId": p["expected_revision_id"]}}).execute()
        return {"document_id": p["document_id"], "replacements": (r.get("replies") or [{}])[0].get("replaceAllText", {}).get("occurrencesChanged", 0)}

    if action == "sheets_update_values":
        q = _sheets().spreadsheets().values()
        existing = q.get(spreadsheetId=p["spreadsheet_id"],range=p["range"],valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [])
        if existing != p["expected_values"]:
            raise InputError("Sheet changed since plan creation; do not overwrite")
        r = q.update(spreadsheetId=p["spreadsheet_id"],range=p["range"],
                     valueInputOption="RAW",body={"values": p["rows"]}).execute()
        readback = q.get(spreadsheetId=p["spreadsheet_id"],range=p["range"],valueRenderOption="UNFORMATTED_VALUE").execute().get("values", [])
        return {"updated_cells": r.get("updatedCells", 0), "readback_matches": readback == p["rows"]}

    if action == "gmail_add_labels":
        # Separate, stronger OAuth permission; never reuse read-only token for writes.
        gmail = service_client(account_alias(), "gmail_write")
        r = gmail.users().messages().modify(userId="me",id=p["message_id"],body={
            "addLabelIds": p["add_label_ids"],"removeLabelIds": p["remove_label_ids"]}).execute()
        return {"message_id": r.get("id"), "label_ids": r.get("labelIds", [])}

    raise InputError("Unimplemented write action")
