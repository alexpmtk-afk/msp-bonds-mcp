"""Read-only archive inventory, Drive identity checks, and Gmail-match helpers.

The registry contains metadata only; document bytes and extracted text are
never persisted. It is intended for local private runtime storage.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from .archive import REPORT_CATEGORIES, VALID_EXTENSIONS
from .classifier import ClassificationResult, classify_document
from .text_extraction import MAX_INPUT_BYTES, ExtractionResult, extract_document_text

MAX_ARCHIVE_FILES = 10_000


def _key(path: str | Path) -> str:
    return Path(str(path).replace("\\", "/")).as_posix().casefold()


def _hash_file(path: Path) -> tuple[str, str, int]:
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            sha256.update(chunk)
            md5.update(chunk)
    return sha256.hexdigest(), md5.hexdigest(), size


def _walk_files(root: Path):
    for category in REPORT_CATEGORIES:
        category_dir = root / category
        if not category_dir.is_dir() or category_dir.is_symlink():
            continue
        stack = [category_dir]
        while stack:
            folder = stack.pop()
            try:
                entries = sorted(folder.iterdir(), key=lambda item: item.name.casefold())
            except OSError:
                continue
            for item in entries:
                if item.is_symlink():
                    continue
                if item.is_dir():
                    stack.append(item)
                elif item.is_file() and item.suffix.casefold() in VALID_EXTENSIONS:
                    yield category, item


def _classification_outcome(expected: str, detected: ClassificationResult,
                            extraction_status: str) -> str:
    if detected.category is not None and detected.category != expected:
        return "FAIL"
    if detected.category is None or extraction_status != "OK" or detected.status == "NEEDS_REVIEW":
        return "NEEDS_REVIEW"
    return "PASS"


def _remote_check(local_sha256: str, local_md5: str, local_size: int,
                  remote: Mapping[str, Any] | None) -> tuple[str, str | None]:
    if not remote:
        return "UNVERIFIED", None
    drive_id = str(remote.get("id") or remote.get("file_id") or "") or None
    remote_md5 = str(remote.get("md5Checksum") or remote.get("md5_checksum") or "").lower()
    remote_sha256 = str(remote.get("sha256") or "").lower()
    remote_size = remote.get("size")
    if remote_size is not None and int(remote_size) != local_size:
        return "MISMATCH", drive_id
    if remote_md5 and remote_md5 != local_md5:
        return "MISMATCH", drive_id
    if remote_sha256 and remote_sha256 != local_sha256:
        return "MISMATCH", drive_id
    if drive_id and remote_size is not None and (remote_md5 or remote_sha256):
        return "MATCH", drive_id
    return "UNVERIFIED", drive_id



def _remote_parent_id(remote: Mapping[str, Any] | None) -> str | None:
    """Return one verified parent folder id; multiple parents stay unresolved."""
    if not remote:
        return None
    direct = remote.get("drive_parent_folder_id") or remote.get("parent_id")
    if direct:
        return str(direct)
    parents = remote.get("parents") or remote.get("parent_ids")
    if isinstance(parents, str):
        parents = [parents]
    if isinstance(parents, (list, tuple)) and len(parents) == 1 and parents[0]:
        return str(parents[0])
    return None


def _period_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    try:
        l_start = date.fromisoformat(left["period_start"])
        l_end = date.fromisoformat(left["period_end"])
        r_start = date.fromisoformat(right["period_start"])
        r_end = date.fromisoformat(right["period_end"])
    except (KeyError, TypeError, ValueError):
        return False
    return max(l_start, r_start) <= min(l_end, r_end)


def _mark_archive_relations(records: list[dict[str, Any]]) -> None:
    by_sha: dict[str, list[dict[str, Any]]] = {}
    by_exact_key: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in records:
        by_sha.setdefault(record["sha256"], []).append(record)
        accounts = tuple(sorted(value.casefold() for value in record["account_ids"]))
        if record["period_start"] and record["period_end"] and record["detected_category"] and accounts:
            key = (record["detected_category"], accounts, record["period_start"], record["period_end"])
            by_exact_key.setdefault(key, []).append(record)
    for group in by_sha.values():
        if len(group) > 1:
            refs = sorted(row["archive_ref"] for row in group)
            for row in group:
                row["exact_hash_duplicate_refs"] = [ref for ref in refs if ref != row["archive_ref"]]
    for group in by_exact_key.values():
        if len(group) > 1:
            refs = sorted(row["archive_ref"] for row in group)
            hashes = {row["sha256"] for row in group}
            for row in group:
                row["same_metadata_refs"] = [ref for ref in refs if ref != row["archive_ref"]]
                row["same_metadata_different_bytes"] = len(hashes) > 1

    # Compare each unique byte sequence once; exact file copies are represented
    # separately by exact_hash_duplicate_refs.
    canonical_by_sha: dict[str, dict[str, Any]] = {}
    for row in records:
        canonical_by_sha.setdefault(row["sha256"], row)
    unique_content = list(canonical_by_sha.values())
    for index, left in enumerate(unique_content):
        left_accounts = tuple(sorted(value.casefold() for value in left["account_ids"]))
        if not left.get("period_start") or not left.get("period_end") or not left.get("detected_category") or not left_accounts:
            continue
        overlaps: list[str] = []
        for right in unique_content[index + 1:]:
            right_accounts = tuple(sorted(value.casefold() for value in right["account_ids"]))
            same_identity = left["detected_category"] == right["detected_category"] and left_accounts == right_accounts
            same_period = (left["period_start"], left["period_end"]) == (right.get("period_start"), right.get("period_end"))
            if same_identity and not same_period and _period_overlap(left, right):
                overlaps.append(right["archive_ref"])
                right.setdefault("overlapping_period_refs", []).append(left["archive_ref"])
        if overlaps:
            left["overlapping_period_refs"] = sorted(overlaps)


def reconcile_archive(
    root: str | Path,
    drive_files_by_relative_path: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Read supported archive files and check verified Drive metadata.

    Metadata can be keyed by relative path or local SHA-256. Each value should
    include a Drive id, size, and either md5Checksum or a SHA-256 computed from
    raw Drive bytes. Missing verification keeps the entry unverified.
    """
    archive_root = Path(root)
    if not archive_root.is_dir():
        return {"status": "UNAVAILABLE", "records": [], "errors": ["Archive root is unavailable"]}
    remote_by_key = {
        _key(path): value for path, value in (drive_files_by_relative_path or {}).items()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(path))
    }
    remote_by_sha = {
        str(path).lower(): value for path, value in (drive_files_by_relative_path or {}).items()
        if re.fullmatch(r"[0-9a-fA-F]{64}", str(path))
    }
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for category, path in _walk_files(archive_root):
        if len(rows) >= MAX_ARCHIVE_FILES:
            errors.append("Archive scan limit reached; result is incomplete")
            break
        relative = path.relative_to(archive_root).as_posix()
        try:
            sha256, md5, size = _hash_file(path)
            if size <= MAX_INPUT_BYTES:
                data = path.read_bytes()
                extraction = extract_document_text(data, filename=path.name, mime_type="")
                result = classify_document(extraction.text, data, filename=path.name, mime_type="")
            else:
                extraction = ExtractionResult("UNKNOWN", "NEEDS_REVIEW", None, "Input exceeds the configured size limit")
                result = ClassificationResult(None, (), None, None, None, None, sha256, 0.0,
                                              "NEEDS_REVIEW", ("INPUT_SIZE_LIMIT",))
        except OSError:
            errors.append("A supported archive file could not be read")
            continue

        remote = remote_by_key.get(_key(relative)) or remote_by_sha.get(sha256)
        drive_status, drive_id = _remote_check(sha256, md5, size, remote)
        row = {
            "archive_ref": f"V{len(rows) + 1:04d}",
            "relative_path": relative,
            "drive_file_id": drive_id,
            "drive_parent_folder_id": _remote_parent_id(remote),
            "drive_verification": drive_status,
            "archive_category": category,
            "detected_category": result.category,
            "classification_status": result.status,
            "classification_outcome": _classification_outcome(category, result, extraction.status),
            "extraction_status": extraction.status,
            "format": extraction.detected_format,
            "account_ids": list(result.account_ids),
            "period_start": result.period_start,
            "period_end": result.period_end,
            "report_date": result.report_date,
            "generated_date": result.generated_date,
            "sha256": sha256,
            "size_bytes": size,
            "pdf_pages": extraction.pages,
            "extracted_characters": extraction.characters,
            "reasons": list(dict.fromkeys([*result.reasons, *([extraction.reason] if extraction.reason else [])])),
            "exact_hash_duplicate_refs": [],
            "same_metadata_refs": [],
            "same_metadata_different_bytes": False,
            "overlapping_period_refs": [],
        }
        rows.append(row)

    rows.sort(key=lambda row: (REPORT_CATEGORIES.index(row["archive_category"]), _key(row["relative_path"])))
    # Stable references after sorting are used by the private registry and report.
    for index, row in enumerate(rows, 1):
        row["archive_ref"] = f"V{index:04d}"
    _mark_archive_relations(rows)
    counts = {status: sum(row["classification_outcome"] == status for row in rows)
              for status in ("PASS", "NEEDS_REVIEW", "FAIL")}
    remote_counts = {status: sum(row["drive_verification"] == status for row in rows)
                     for status in ("MATCH", "UNVERIFIED", "MISMATCH")}
    status = "OK" if not errors and len(rows) > 0 and remote_counts["MATCH"] == len(rows) else "PARTIAL"
    return {"status": status, "total": len(rows), "classification_counts": counts,
            "drive_verification_counts": remote_counts, "records": rows, "errors": errors}


def write_archive_registry(path: str | Path, registry: Mapping[str, Any]) -> Path:
    """Persist metadata-only registry atomically to a local private path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    payload = json.dumps(registry, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, target)
    return target



def account_set_route_key(category: str | None, account_ids: Sequence[str] | None) -> str | None:
    """Return a private, order-independent key for category plus the full account set."""
    category_key = str(category or "").strip().casefold()
    if not category_key or not account_ids or isinstance(account_ids, (str, bytes)):
        return None
    accounts = sorted({
        str(value).strip().casefold()
        for value in account_ids
        if value is not None and str(value).strip()
    })
    if not accounts:
        return None
    payload = json.dumps(
        [category_key, accounts], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_folder_route_index(records: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, ...]]:
    """Build routes only from classified, Drive-verified records with a known parent id.

    A key mapped to more than one folder is deliberately ambiguous.
    """
    evidence: dict[str, set[str]] = {}
    for row in records:
        if (
            row.get("drive_verification") != "MATCH"
            or row.get("classification_status") != "CLASSIFIED"
            or row.get("classification_outcome") != "PASS"
            or not row.get("drive_file_id")
        ):
            continue
        folder_id = str(row.get("drive_parent_folder_id") or "").strip()
        key = account_set_route_key(row.get("detected_category"), row.get("account_ids"))
        if folder_id and key:
            evidence.setdefault(key, set()).add(folder_id)
    return {key: tuple(sorted(folder_ids)) for key, folder_ids in sorted(evidence.items())}


def _target_folder_result(
    status: str,
    reason: str,
    *,
    folder_ids: Sequence[str] = (),
    drive_file_ids: Sequence[str] = (),
    archive_refs: Sequence[str] = (),
) -> dict[str, Any]:
    unique_folders = sorted(set(folder_ids))
    return {
        "status": status,
        "reason": reason,
        "target_folder_id": unique_folders[0] if len(unique_folders) == 1 else None,
        "target_folder_ids": unique_folders,
        "drive_file_ids": sorted({str(value) for value in drive_file_ids if value}),
        "archive_refs": sorted({str(value) for value in archive_refs if value}),
    }


def resolve_target_folder(
    candidate: ClassificationResult,
    archive_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Check archived SHA-256 first, then route by verified category and full account set.

    This function is read-only. It never creates folders or writes archive files.
    """
    digest = str(getattr(candidate, "sha256", "") or "").casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return _target_folder_result("NEEDS_REVIEW", "INVALID_SHA256")

    hash_matches = [
        row for row in archive_records
        if str(row.get("sha256") or "").casefold() == digest
    ]
    if hash_matches:
        verified = [
            row for row in hash_matches
            if row.get("drive_verification") == "MATCH" and row.get("drive_file_id")
        ]
        if not verified:
            return _target_folder_result(
                "NEEDS_REVIEW",
                "HASH_MATCH_NOT_DRIVE_VERIFIED",
                archive_refs=[row.get("archive_ref") for row in hash_matches],
            )
        if any(not row.get("drive_parent_folder_id") for row in verified):
            return _target_folder_result(
                "NEEDS_REVIEW",
                "HASH_MATCH_LOCATION_UNKNOWN",
                drive_file_ids=[row.get("drive_file_id") for row in verified],
                archive_refs=[row.get("archive_ref") for row in verified],
            )
        return _target_folder_result(
            "EXACT_DUPLICATE",
            "SHA256_ALREADY_IN_ARCHIVE",
            folder_ids=[row["drive_parent_folder_id"] for row in verified],
            drive_file_ids=[row.get("drive_file_id") for row in verified],
            archive_refs=[row.get("archive_ref") for row in verified],
        )

    if (
        getattr(candidate, "status", None) != "CLASSIFIED"
        or not getattr(candidate, "category", None)
        or not getattr(candidate, "account_ids", ())
    ):
        return _target_folder_result("NEEDS_REVIEW", "CLASSIFICATION_OR_ACCOUNT_INCOMPLETE")

    route_key = account_set_route_key(candidate.category, candidate.account_ids)
    if not route_key:
        return _target_folder_result("NEEDS_REVIEW", "CLASSIFICATION_OR_ACCOUNT_INCOMPLETE")
    route_index = build_folder_route_index(archive_records)
    folder_ids = route_index.get(route_key, ())
    if len(folder_ids) == 1:
        return _target_folder_result("READY", "CONFIRMED_ACCOUNT_SET_ROUTE", folder_ids=folder_ids)
    if len(folder_ids) > 1:
        return _target_folder_result(
            "NEEDS_REVIEW",
            "ACCOUNT_SET_EXISTS_IN_MULTIPLE_FOLDERS",
            folder_ids=folder_ids,
        )
    # Match stable accounts against independently verified archived documents.
    evidence: dict[str, dict[str, set[str]]] = {}
    for row in archive_records:
        if not (row.get("drive_verification") == "MATCH" and row.get("classification_status") == "CLASSIFIED" and row.get("classification_outcome") == "PASS" and row.get("detected_category") == candidate.category and row.get("drive_file_id") and row.get("drive_parent_folder_id")):
            continue
        for account in row.get("account_ids") or ():
            key = str(account).strip().casefold()
            if key:
                evidence.setdefault(key, {}).setdefault(str(row["drive_parent_folder_id"]), set()).add(str(row["sha256"]))
    proven = set()
    for account in candidate.account_ids:
        folders = evidence.get(str(account).strip().casefold(), {})
        if len(folders) == 1:
            folder, hashes = next(iter(folders.items()))
            if len(hashes) >= 2:
                proven.add(folder)
    if len(proven) == 1:
        return _target_folder_result("READY", "VERIFIED_ACCOUNT_ROUTE", folder_ids=sorted(proven))
    if len(proven) > 1:
        return _target_folder_result("NEEDS_REVIEW", "CONFLICTING_ACCOUNT_ROUTES", folder_ids=sorted(proven))
    return _target_folder_result("NEEDS_REVIEW", "NO_CONFIRMED_ACCOUNT_SET_ROUTE")


def match_archive_candidate(candidate: ClassificationResult,
                            records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Match a Gmail candidate to archive metadata without changing either source."""
    exact = [row for row in records if row.get("sha256") == candidate.sha256]
    if exact:
        verified = [row for row in exact if row.get("drive_verification") == "MATCH" and row.get("drive_file_id")]
        return {"status": "EXACT_DUPLICATE" if verified else "EXACT_HASH_UNVERIFIED",
                "archive_refs": [row.get("archive_ref") for row in verified or exact],
                "drive_file_ids": [row.get("drive_file_id") for row in verified]}
    if not candidate.category or not candidate.account_ids or not candidate.period_start or not candidate.period_end:
        return {"status": "NEEDS_REVIEW", "archive_refs": [], "drive_file_ids": []}

    candidate_accounts = tuple(sorted(value.casefold() for value in candidate.account_ids))
    same_identity = [row for row in records
                     if row.get("drive_verification") == "MATCH"
                     and row.get("detected_category") == candidate.category
                     and tuple(sorted(value.casefold() for value in row.get("account_ids", []))) == candidate_accounts]
    same_period = [row for row in same_identity
                   if row.get("period_start") == candidate.period_start
                   and row.get("period_end") == candidate.period_end]
    if same_period:
        return {"status": "POSSIBLE_VERSION", "archive_refs": [row.get("archive_ref") for row in same_period],
                "drive_file_ids": [row.get("drive_file_id") for row in same_period]}
    overlapping = [row for row in same_identity if _period_overlap(
        {"period_start": candidate.period_start, "period_end": candidate.period_end}, row)]
    if overlapping:
        return {"status": "OVERLAPPING_PERIOD", "archive_refs": [row.get("archive_ref") for row in overlapping],
                "drive_file_ids": [row.get("drive_file_id") for row in overlapping]}
    return {"status": "NEW_DOCUMENT", "archive_refs": [], "drive_file_ids": []}
