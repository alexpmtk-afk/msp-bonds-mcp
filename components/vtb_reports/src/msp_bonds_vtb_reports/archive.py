"""Safe inventory and signature checks for a user-configured report archive.

No reports are modified, moved or imported. No broker data are parsed.
The configured root is mandatory: there is no search across local drives.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

ENV_REPORT_ROOT = "MSP_BONDS_REPORTS_ROOT"
REPORT_CATEGORIES = (
    "Брокерский отчёт",
    "Выписка по счёту депо",
    "Отчёт о выплатах по ценным бумагам",
)
VALID_EXTENSIONS = frozenset((".pdf", ".xlsx"))
MAX_LIST_LIMIT = 500
MAX_SCAN_FILES = 10_000


def archive_root() -> Path | None:
    """Resolve only the explicitly configured folder (do not discover drives)."""
    raw = os.environ.get(ENV_REPORT_ROOT, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _scan_account(account_path: Path, report_type: str, account: str,
                  files: list[dict[str, Any]], errors: list[str]) -> None:
    """Walk an account folder without following reparse-point/symlink entries."""
    stack = [account_path]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name.casefold())
        except OSError as exc:
            errors.append(f"Unable to inspect account folder ({report_type}/{account}): {exc.__class__.__name__}")
            continue
        for entry in children:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                ext = Path(entry.name).suffix.lower()
                if ext not in VALID_EXTENSIONS:
                    continue
                if len(files) >= MAX_SCAN_FILES:
                    errors.append(f"Inventory limit of {MAX_SCAN_FILES} reached; result is incomplete")
                    return
                stat = entry.stat(follow_symlinks=False)
                files.append({
                    "report_type": report_type,
                    "account_folder": account,
                    "name": entry.name,
                    "relative_path": str(Path(entry.path).relative_to(account_path.parent)),
                    "format": ext.lstrip(".").upper(),
                    "size_bytes": stat.st_size,
                    "_absolute_path": Path(entry.path),
                })
            except OSError as exc:
                errors.append(f"Cannot stat entry in {report_type}/{account}: {exc.__class__.__name__}")


def inventory() -> dict[str, Any]:
    """Inspect 3 report sections and discover account folders dynamically."""
    root = archive_root()
    if root is None:
        return {"status": "NOT_CONFIGURED", "summary": [], "files": [], "errors": [f"Set {ENV_REPORT_ROOT} in the MCP process environment"]}
    if not root.is_dir():
        return {"status": "UNAVAILABLE", "summary": [], "files": [], "errors": ["Configured archive root is not an accessible directory"]}
    files: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    errors: list[str] = []
    for report_type in REPORT_CATEGORIES:
        parent = root / report_type
        if not parent.is_dir():
            summary.append({"report_type": report_type, "present": False, "accounts": [], "file_count": 0})
            errors.append(f"Report section unavailable: {report_type}")
            continue
        try:
            with os.scandir(parent) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name.casefold())
            accounts = [entry.name for entry in children if entry.is_dir(follow_symlinks=False) and not entry.is_symlink()]
        except OSError as exc:
            summary.append({"report_type": report_type, "present": True, "accounts": [], "file_count": 0})
            errors.append(f"Cannot list report section {report_type}: {exc.__class__.__name__}")
            continue
        initial = len(files)
        for account in accounts:
            _scan_account(parent / account, report_type, account, files, errors)
        summary.append({"report_type": report_type, "present": True, "accounts": accounts,
                        "file_count": len(files) - initial})
    files.sort(key=lambda entry: (entry["report_type"], entry["account_folder"], entry["relative_path"].casefold()))
    status = "OK" if not errors else "PARTIAL"
    return {"status": status, "summary": summary, "files": files, "errors": errors}


def archive_status() -> dict[str, Any]:
    data = inventory()
    return {"status": data["status"], "sections": data["summary"],
            "total_files": len(data["files"]), "errors": data["errors"],
            "writes_enabled": False}


def list_reports(report_type: str = "", account: str = "", offset: int = 0,
                 limit: int = 100) -> dict[str, Any]:
    """List report file metadata, never read financial values."""
    if report_type and report_type not in REPORT_CATEGORIES:
        return {"status": "INVALID_FILTER", "error": "Unknown report_type", "reports": []}
    if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_LIMIT:
        return {"status": "INVALID_FILTER", "error": "offset must be >=0 and limit must be 1..500", "reports": []}
    data = inventory()
    found = [item for item in data["files"] if (not report_type or item["report_type"] == report_type)
             and (not account or item["account_folder"] == account)]
    selected = [{key: value for key, value in item.items() if key != "_absolute_path"}
                for item in found[offset:offset + limit]]
    return {"status": data["status"], "total_matches": len(found), "offset": offset,
            "returned": len(selected), "reports": selected, "errors": data["errors"]}


def detect_format(path: Path) -> str:
    """Verify a PDF signature or XLSX ZIP structure; never extract contents."""
    try:
        with path.open("rb") as stream:
            head = stream.read(8)
        ext = path.suffix.lower()
        if ext == ".pdf":
            return "PASS" if head.startswith(b"%PDF-") else "FAIL"
        if ext == ".xlsx":
            if not head.startswith(b"PK"):
                return "FAIL"
            with ZipFile(path) as workbook:
                names = set(workbook.namelist())
            return "PASS" if "xl/workbook.xml" in names and "[Content_Types].xml" in names else "FAIL"
        return "UNSUPPORTED"
    except (OSError, BadZipFile, ValueError, RuntimeError):
        return "ERROR"


def check_report_files(max_files: int = 30) -> dict[str, Any]:
    """Check signatures of up to max_files without modifying files."""
    if not isinstance(max_files, int) or not 1 <= max_files <= MAX_LIST_LIMIT:
        return {"status": "INVALID_FILTER", "error": "max_files must be 1..500"}
    data = inventory()
    outcomes: dict[str, int] = {"PASS": 0, "FAIL": 0, "ERROR": 0}
    issues: list[dict[str, str]] = []
    for item in data["files"][:max_files]:
        outcome = detect_format(item["_absolute_path"])
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if outcome != "PASS":
            issues.append({"report_type": item["report_type"],
                           "account_folder": item["account_folder"],
                           "name": item["name"], "result": outcome})
    return {"status": data["status"], "checked": min(max_files, len(data["files"])),
            "total_files": len(data["files"]), "results": outcomes,
            "issues": issues, "errors": data["errors"], "writes_enabled": False}
