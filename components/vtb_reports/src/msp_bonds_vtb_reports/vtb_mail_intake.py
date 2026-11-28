"""HOME-only, read-only scheduled VTB discovery and explicit local document approval.

The periodic `scan` never writes to Google Drive or to the report archive.
It stores only private intake metadata and exact attachments in existing staging.
Each remote write requires a separate interactive local approval by the owner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable

from .archive_reconciliation import match_archive_candidate, resolve_target_folder
from .classifier import ClassificationResult, classify_document
from .gmail_discovery import load_archive_registry, normalize_sender_allowlist, scan_gmail_reports
from .text_extraction import extract_document_text

SCHEMA = 1
LABEL_ID = "CONFIGURE_GMAIL_LABEL_ID"
LABEL_NAME = "ВТБ/БрокерВТБ"
CORPORATE_LABEL_NAME = "ВТБ/Уведомление о КД"
ALLOWED_STATES = {"PENDING_APPROVAL", "UPLOADED_PENDING_SYNC", "UPLOAD_UNCERTAIN", "ALREADY_ARCHIVED", "NEEDS_REVIEW"}
MAX_QUEUE = 200
CACHE_DAYS = 7
MAX_MESSAGE_CACHE = 5000



@contextmanager
def _locked_queue(path: Path):
    """One foreground approval/scan at a time; OS releases lock after a crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        locked = False
        deadline = time.monotonic() + 3
        while not locked:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError("INTAKE_LOCK_BUSY") from exc
                time.sleep(0.1)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _runtime_path() -> Path:
    from msp_bonds_mcp.core.paths import runtime_dir
    return runtime_dir() / "vtb_gmail_intake.json"


def _registry_path() -> Path:
    from msp_bonds_mcp.core.paths import runtime_dir
    return runtime_dir() / "vtb_archive_registry.json"


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": SCHEMA, "items": [], "scanned_at": "", "last_scan": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA or not isinstance(payload.get("items"), list):
        raise ValueError("INTAKE_STATE_INVALID")
    if len(payload["items"]) > MAX_QUEUE:
        raise ValueError("INTAKE_QUEUE_LIMIT")
    if any(not isinstance(item, dict) or item.get("state") not in ALLOWED_STATES for item in payload["items"]):
        raise ValueError("INTAKE_ITEM_INVALID")
    return payload


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    if temp.exists():
        raise ValueError("INTAKE_PENDING_TEMP_FILE_REVIEW_REQUIRED")
    try:
        with temp.open("x", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _existing_archive_hashes(rows: list[dict[str, Any]]) -> set[str]:
    return {r["sha256"] for r in rows if r.get("drive_verification") == "MATCH" and _valid_digest(r.get("sha256"))}


def _trusted_folders(rows: list[dict[str, Any]]) -> set[str]:
    return {str(row["drive_parent_folder_id"]) for row in rows if row.get("drive_verification") == "MATCH" and row.get("drive_parent_folder_id")}


def _staged_valid(item: dict[str, Any], stage_root: Path) -> bool:
    name = item.get("staged_name")
    digest = item.get("sha256")
    if not name or not isinstance(name, str) or name != Path(name).name or not _valid_digest(digest):
        return False
    path = stage_root / name
    if not path.is_file() or path.is_symlink():
        return False
    if path.stat().st_size != item.get("size_bytes"):
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == digest


def merge_scan_into_queue(
    *,
    state: dict[str, Any],
    scan: dict[str, Any],
    archive_records: list[dict[str, Any]],
    stage: Callable[[str, str], dict[str, Any]],
    stage_root: Path,
) -> dict[str, int]:
    """Merge scanner results; stage only exact NEW documents in known folders."""
    if scan.get("status") not in {"OK", "PARTIAL"}:
        raise ValueError("GMAIL_SCAN_UNAVAILABLE")
    if not scan.get("archive_registry_complete", False):
        raise ValueError("ARCHIVE_REGISTRY_UNVERIFIED")
    hashes = _existing_archive_hashes(archive_records)
    folders = _trusted_folders(archive_records)
    by_sha: dict[str, dict[str, Any]] = {i["sha256"]: i for i in state["items"] if _valid_digest(i.get("sha256"))}
    staged = 0
    archived = 0
    skipped = 0
    problems = 0
    for item in state["items"]:
        if item.get("sha256") in hashes and item["state"] not in {
                "ALREADY_ARCHIVED", "UPLOAD_UNCERTAIN", "UPLOADED_PENDING_SYNC"}:
            item["state"] = "ALREADY_ARCHIVED"
            archived += 1
    for row in scan.get("results") or []:
        if row.get("status") != "NEW":
            continue
        digest = row.get("sha256")
        part_id = str(row.get("part_id") or "")
        gmail_id = str(row.get("gmail_message_id") or "")
        folder = row.get("target_folder_id")
        filename = str(row.get("filename") or "")
        if (not _valid_digest(digest) or not gmail_id or not part_id
                or folder not in folders or not filename or Path(filename).name != filename
                or "\\" in filename or Path(filename).suffix.casefold() not in {".pdf", ".xlsx"}
                or len(state["items"]) >= MAX_QUEUE):
            problems += 1
            continue
        if digest in hashes:
            archived += 1
            continue
        current = by_sha.get(digest)
        if current:
            if current["state"] in {"ALREADY_ARCHIVED", "UPLOADED_PENDING_SYNC", "UPLOAD_UNCERTAIN"}:
                skipped += 1
                continue
            if current.get("target_folder_id") != folder or current.get("filename") != filename:
                current["state"] = "NEEDS_REVIEW"
                current["reason"] = "DUPLICATE_HASH_DIFFERENT_DESTINATION"
                problems += 1
                continue
            if _staged_valid(current, stage_root):
                skipped += 1
                continue
        staged_data = stage(gmail_id, part_id)
        if (staged_data.get("sha256") != digest
                or staged_data.get("size") != row.get("size_bytes")
                or staged_data.get("suggested_name") != filename):
            problems += 1
            continue
        candidate = {
            "sha256": digest, "filename": filename, "size_bytes": row["size_bytes"],
            "gmail_message_id": gmail_id, "part_id": part_id,
            "category": row.get("category"), "account_ids": row.get("account_ids") or [],
            "target_folder_id": folder, "staged_name": staged_data.get("staged_name"),
            "state": "PENDING_APPROVAL", "discovered_at": _now(),
        }
        if not _staged_valid(candidate, stage_root):
            problems += 1
            continue
        if current:
            current.update(candidate)
        else:
            state["items"].append(candidate)
            by_sha[digest] = candidate
        staged += 1
    return {"staged": staged, "already_archived": archived, "previously_queued": skipped, "problems": problems}


def _scan_live(state: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], set[str]]:
    from msp_bonds_google import workspace as gw
    from msp_bonds_google.auth import token_state
    account = gw.account_alias()
    if not token_state(account, "gmail").get("configured"):
        raise ValueError("GMAIL_READONLY_OAUTH_NOT_CONFIGURED")
    labels = gw.gmail_list_labels().get("labels") or []
    broker = [l for l in labels if l.get("id") == LABEL_ID and l.get("name") == LABEL_NAME]
    corporate = [l for l in labels if l.get("name") == CORPORATE_LABEL_NAME and l.get("id")]
    if len(broker) != 1 or len(corporate) != 1:
        raise ValueError("VTB_GMAIL_LABELS_NOT_VERIFIED")
    archive_records, complete, reason = load_archive_registry(_registry_path())
    if not complete:
        raise ValueError("ARCHIVE_REGISTRY_UNVERIFIED:" + reason)
    allowed = normalize_sender_allowlist(os.environ.get("MSP_VTB_ALLOWED_SENDERS") or "project-address-03@example.invalid")
    if not allowed or allowed != frozenset({"project-address-03@example.invalid"}):
        raise ValueError("VTB_ALLOWLIST_REQUIRES_EXPLICIT_REVIEW")
    now = datetime.now(timezone.utc)
    cache = state.get("seen_messages") or {}
    if not isinstance(cache, dict):
        raise ValueError("MESSAGE_CACHE_INVALID")
    cached = set()
    for mid, checked_at in cache.items():
        try:
            checked = datetime.fromisoformat(checked_at)
            if checked.tzinfo and now - checked < timedelta(days=CACHE_DAYS):
                cached.add(mid)
        except (TypeError, ValueError):
            pass
    fetched: set[str] = set()
    def get_message(message_id: str) -> dict[str, Any]:
        if message_id in cached:
            # The email's contents were classified recently; queue state is durable.
            return {"id": message_id, "labels": [],
                    "headers": {"from": "project-address-03@example.invalid"}, "attachments": []}
        fetched.add(message_id)
        return gw.gmail_message(message_id)
    result = scan_gmail_reports(
        search_page=gw.gmail_search,
        get_message=get_message,
        get_attachment=lambda mid, part, limit: gw.gmail_attachment_bytes(mid, part, max_bytes=limit),
        archive_records=archive_records, archive_complete=True,
        allowed_senders=allowed, broker_label_id=LABEL_ID,
        corporate_action_label_id=corporate[0]["id"],
        scan_sender_mailbox=True, page_size=100, max_pages=20,
    )
    return result, archive_records, fetched


def scan_command() -> int:
    from msp_bonds_google import workspace as gw
    path = _runtime_path()
    state = _read_state(path)
    result, records, fetched = _scan_live(state)
    # We require complete pagination to call this a finished scan.
    if result.get("next_page_token"):
        print("INTAKE=PARTIAL SCAN_PAGINATION_INCOMPLETE")
        return 2
    merged = merge_scan_into_queue(
        state=state, scan=result, archive_records=records,
        stage=gw.gmail_stage_attachment, stage_root=gw.staging_dir(),
    )
    stamp = _now()
    cache = state.setdefault("seen_messages", {})
    failed_ids = {str(r.get("gmail_message_id") or "") for r in result.get("results") or []
                  if r.get("status") == "ERROR"}
    for mid in fetched - failed_ids:
        cache[mid] = stamp
    if len(cache) > MAX_MESSAGE_CACHE:
        ordered = sorted(cache.items(), key=lambda item: str(item[1]), reverse=True)
        state["seen_messages"] = dict(ordered[:MAX_MESSAGE_CACHE])
    state["scanned_at"] = stamp
    state["last_scan"] = {"status": result.get("status"), "counts": result.get("result_counts"), **merged}
    _write_state(path, state)
    if result.get("errors") or (result.get("result_counts") or {}).get("ERROR", 0):
        print("INTAKE=PARTIAL ERRORS=" + str(len(result.get("errors") or [])
              + (result.get("result_counts") or {}).get("ERROR", 0))
              + " NEW_STAGED=" + str(merged["staged"]))
        return 2
    print("INTAKE=OK NEW_STAGED=" + str(merged["staged"])
          + " NEEDS_REVIEW=" + str((result.get("result_counts") or {}).get("NEEDS_REVIEW", 0) + merged["problems"])
          + " PENDING_APPROVAL=" + str(sum(i["state"] == "PENDING_APPROVAL" for i in state["items"])))
    return 0


def status_command() -> int:
    state = _read_state(_runtime_path())
    print("INTAKE_STATUS scanned_at=" + str(state.get("scanned_at") or "NEVER")
          + " entries=" + str(len(state["items"])))
    for i in state["items"]:
        print(i.get("sha256", "")[:12], i["state"], i.get("category"), i.get("filename"))
    return 0


def _select(state: dict[str, Any], prefix: str) -> dict[str, Any]:
    if not prefix or len(prefix) < 12 or not re.fullmatch(r"[0-9a-f]{12,64}", prefix):
        raise ValueError("REQUIRE_SHA256_PREFIX_AT_LEAST_12_HEX")
    matches = [i for i in state["items"] if i.get("sha256", "").startswith(prefix)]
    if len(matches) != 1:
        raise ValueError("DOCUMENT_NOT_UNIQUE_OR_MISSING")
    return matches[0]



def _conflict_with_verified_version(
    candidate: ClassificationResult, filename: str, folder_id: str,
    records: list[dict[str, Any]],
) -> bool:
    """Fail closed on conflicting same-period or same-date reports in archive.

    Existing files can contain incidental account IDs; identify one account that
    is uniquely supported by >=2 verified documents in the same destination.
    Filename date is only a collision guard, never classification evidence.
    """
    evidence: dict[str, dict[str, set[str]]] = {}
    for row in records:
        if (row.get("drive_verification") != "MATCH"
                or row.get("detected_category") != candidate.category):
            continue
        parent = str(row.get("drive_parent_folder_id") or "")
        digest = str(row.get("sha256") or "")
        for account in row.get("account_ids") or []:
            key = str(account).strip().casefold()
            if key and parent and digest:
                evidence.setdefault(key, {}).setdefault(parent, set()).add(digest)
    primary = [str(a).strip().casefold() for a in candidate.account_ids
               if set(evidence.get(str(a).strip().casefold(), {})) == {folder_id}
               and len(evidence[str(a).strip().casefold()][folder_id]) >= 2]
    if len(set(primary)) != 1:
        raise ValueError("DOCUMENT_PRIMARY_ACCOUNT_NOT_UNIQUE")
    primary_id = primary[0]
    if candidate.category == "Выписка по счёту депо":
        match = re.search(r"за\s+(\d{2}\.\d{2}\.\d{4})\.(?:xlsx|pdf)$", filename, re.I)
        if match is None:
            raise ValueError("DEPOT_STATEMENT_DATE_NOT_IN_FILENAME")
        date_label = match.group(1)
        for row in records:
            if (row.get("drive_verification") == "MATCH"
                    and row.get("detected_category") == candidate.category
                    and primary_id in [str(a).strip().casefold() for a in row.get("account_ids") or []]
                    and re.search(r"за\s+" + re.escape(date_label) + r"\.(?:xlsx|pdf)$",
                                  str(row.get("relative_path") or ""), re.I)
                    and row.get("sha256") != candidate.sha256):
                return True
        return False
    if candidate.period_start and candidate.period_end:
        for row in records:
            if (row.get("drive_verification") == "MATCH"
                    and row.get("detected_category") == candidate.category
                    and primary_id in [str(a).strip().casefold() for a in row.get("account_ids") or []]
                    and (row.get("period_start"), row.get("period_end")) ==
                    (candidate.period_start, candidate.period_end)
                    and row.get("sha256") != candidate.sha256):
                return True
        return False
    raise ValueError("DOCUMENT_PERIOD_NOT_CONFIRMED")


def reconcile_uncertain_item(item: dict[str, Any], *, stage_root: Path,
                             archive_records: list[dict[str, Any]],
                             lookup: Callable[[str, str], dict | None],
                             readback: Callable[[str, bytes], dict]) -> str:
    """Read-only remote recovery. No missing-file result authorizes a retry."""
    if item.get("state") != "UPLOAD_UNCERTAIN":
        raise ValueError("NOT_UPLOAD_UNCERTAIN")
    parent = item.get("target_folder_id")
    name = item.get("filename")
    if (parent not in _trusted_folders(archive_records) or not isinstance(name, str)
            or not name or Path(name).name != name or "\\" in name
            or Path(name).suffix.casefold() not in {".pdf", ".xlsx"}
            or not _staged_valid(item, stage_root)):
        return "UNCERTAIN_LOCAL_EVIDENCE_INVALID"
    content = (stage_root / item["staged_name"]).read_bytes()
    if hashlib.sha256(content).hexdigest() != item["sha256"]:
        return "UNCERTAIN_LOCAL_EVIDENCE_INVALID"
    try:
        meta = lookup(parent, name)
        if not meta:
            return "UNCERTAIN_NOT_FOUND_NO_RETRY"
        if (not meta.get("id") or meta.get("name") != name
                or meta.get("parents") != [parent]
                or int(meta.get("size", -1)) != len(content)):
            return "UNCERTAIN_REMOTE_IDENTITY_MISMATCH"
        verified = readback(str(meta["id"]), content)
        if verified.get("sha256_verified") is not True or verified.get("sha256") != item["sha256"]:
            return "UNCERTAIN_REMOTE_HASH_MISMATCH"
    except Exception:
        return "UNCERTAIN_REMOTE_CHECK_FAILED"
    item["state"] = "UPLOADED_PENDING_SYNC"
    item["drive_file_id"] = str(meta["id"])
    item.pop("reason", None)
    return "UPLOADED_PENDING_SYNC"


def reconcile_command(prefix: str) -> int:
    """Reconcile an uncertain upload without creating or modifying Drive files."""
    from msp_bonds_google import workspace as gw
    from msp_bonds_google.identity import assert_same_account
    from msp_bonds_google.writes import _readback
    from .vtb_registry_refresh import _drive_exact_file, refresh_vtb_registry
    p = _runtime_path()
    state = _read_state(p)
    item = _select(state, prefix)
    rows, complete, _ = load_archive_registry(_registry_path())
    if not complete:
        raise ValueError("ARCHIVE_REGISTRY_UNVERIFIED")
    assert_same_account()
    status = reconcile_uncertain_item(item, stage_root=gw.staging_dir(),
                                      archive_records=rows, lookup=_drive_exact_file,
                                      readback=_readback)
    if status == "UPLOADED_PENDING_SYNC":
        _write_state(p, state)
        try:
            refresh_vtb_registry(target_parent_id=item["target_folder_id"],
                                 expected_name=item["filename"],
                                 expected_file_id=item["drive_file_id"],
                                 expected_sha256=item["sha256"])
        except Exception:
            pass  # Verified remote upload remains pending local registry sync.
    print("INTAKE=" + status)
    return 0 if status == "UPLOADED_PENDING_SYNC" else 2


def approve_command(prefix: str) -> int:
    """Only explicit foreground terminal approval may upload a single document."""
    if not sys.stdin.isatty():
        raise ValueError("LOCAL_INTERACTIVE_TTY_REQUIRED")
    from msp_bonds_google import workspace as gw
    from msp_bonds_google.approvals import approve_interactively, propose
    from msp_bonds_google.utils import safe_filename
    from msp_bonds_google.writes import execute
    p = _runtime_path()
    state = _read_state(p)
    item = _select(state, prefix)
    if item["state"] != "PENDING_APPROVAL":
        raise ValueError("NOT_PENDING_APPROVAL")
    rows, complete, reason = load_archive_registry(_registry_path())
    if not complete:
        raise ValueError("ARCHIVE_REGISTRY_UNVERIFIED:" + reason)
    if item["sha256"] in _existing_archive_hashes(rows):
        item["state"] = "ALREADY_ARCHIVED"
        _write_state(p, state)
        print("INTAKE=ALREADY_ARCHIVED")
        return 0
    if not _staged_valid(item, gw.staging_dir()):
        raise ValueError("STAGED_CONTENT_CHANGED")
    if safe_filename(item["filename"]) != item["filename"]:
        raise ValueError("UNSAFE_FILENAME")
    # Re-extract and classify the staged bytes. No trust in mutable queue metadata.
    content = (gw.staging_dir() / item["staged_name"]).read_bytes()
    extracted = extract_document_text(content, filename=item["filename"])
    if extracted.status != "OK" or not extracted.text:
        raise ValueError("CONTENT_NOT_EXTRACTED")
    if extracted.detected_format.casefold() != Path(item["filename"]).suffix.lstrip(".").casefold():
        raise ValueError("DOCUMENT_FORMAT_FILENAME_MISMATCH")
    c: ClassificationResult = classify_document(extracted.text, content, filename=item["filename"])
    target = resolve_target_folder(c, rows)
    match = match_archive_candidate(c, rows)
    if (c.status != "CLASSIFIED" or c.category != item["category"]
            or sorted(a.casefold() for a in c.account_ids) != sorted(a.casefold() for a in item["account_ids"])
            or target.get("status") != "READY"
            or target.get("target_folder_id") != item["target_folder_id"]
            or match.get("status") in {"POSSIBLE_VERSION", "EXACT_HASH_UNVERIFIED", "EXACT_DUPLICATE"}):
        raise ValueError("DOCUMENT_OR_DESTINATION_CHANGED_NEEDS_REVIEW")
    if _conflict_with_verified_version(c, item["filename"], item["target_folder_id"], rows):
        raise ValueError("DOCUMENT_VERSION_CONFLICT")
    message = gw.gmail_message(item["gmail_message_id"])
    from .gmail_discovery import _sender_status
    if not _sender_status(str(message.get("headers", {}).get("from") or ""),frozenset({"project-address-03@example.invalid"}))[0]:
        raise ValueError("GMAIL_SENDER_NO_LONGER_TRUSTED")
    if not any(a.get("part_id") == item["part_id"] and a.get("filename") == item["filename"] for a in (message.get("attachments") or [])):
        raise ValueError("GMAIL_ATTACHMENT_IDENTITY_CHANGED")
    params = {"parent_id": item["target_folder_id"], "staged_name": item["staged_name"],
              "name": item["filename"], "expected_sha256": item["sha256"]}
    plan = propose("drive_upload_staged", json.dumps(params,ensure_ascii=False))
    answer = approve_interactively(plan["plan_id"])
    if not answer.get("approved"):
        print("INTAKE=NOT_APPROVED")
        return 1
    # Persist uncertainty before making the already locally approved network call.
    # Crash or transport failure must never reset this entry to PENDING_APPROVAL.
    item["state"] = "UPLOAD_UNCERTAIN"
    item["reason"] = "REMOTE_UPLOAD_UNCERTAIN_NO_AUTOMATIC_RETRY"
    _write_state(p, state)
    previous = os.environ.get("MSP_GOOGLE_WRITES_ENABLED")
    try:
        os.environ["MSP_GOOGLE_WRITES_ENABLED"] = "1"
        reply = execute(plan["plan_id"])
    finally:
        if previous is None:
            os.environ.pop("MSP_GOOGLE_WRITES_ENABLED", None)
        else:
            os.environ["MSP_GOOGLE_WRITES_ENABLED"] = previous
    if reply.get("status") == "VERIFIED" and reply.get("sha256_verified") and reply.get("sha256") == item["sha256"] and reply.get("file_id"):
        item["state"] = "UPLOADED_PENDING_SYNC"
        item["drive_file_id"] = reply.get("file_id")
        item.pop("reason", None)
        _write_state(p, state)
        print("INTAKE=UPLOADED_VERIFIED STATUS=AWAITING_REGISTRY_SYNC")
        return 0
    item["state"] = "UPLOAD_UNCERTAIN"
    item["reason"] = "REMOTE_UPLOAD_UNCERTAIN_NO_AUTOMATIC_RETRY"
    _write_state(p, state)
    print("INTAKE=NEEDS_REVIEW REMOTE_UPLOAD_UNCERTAIN")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="VTB HOME Gmail queue. Scheduled scan never uploads.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("scan")
    commands.add_parser("status")
    approve = commands.add_parser("approve")
    approve.add_argument("sha256_prefix")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("sha256_prefix")
    args = parser.parse_args()
    try:
        if args.command == "status":
            return status_command()
        with _locked_queue(_runtime_path()):
            if args.command == "scan":
                return scan_command()
            if args.command == "reconcile":
                return reconcile_command(args.sha256_prefix)
            return approve_command(args.sha256_prefix)
    except Exception as exc:
        # Never log provider exceptions, tokens or private document text.
        print("INTAKE=ERROR TYPE=" + type(exc).__name__ + " CODE=" + (
            str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[A-Z0-9_:]+", str(exc)) else "REDACTED"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
