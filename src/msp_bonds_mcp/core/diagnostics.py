"""Bounded, read-only source fingerprints for the installed HOME MCP.

This deliberately exposes neither arbitrary file access nor file contents.
It does not read the runtime directory, Google credentials, account settings,
financial reports, environment values, or process command lines.
"""

from __future__ import annotations

import ast
import hashlib
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

# Every permitted directory is source code or synthetic tests. Do not add
# runtime/, configuration directories, user data folders or generic paths.
_SOURCE_GROUPS: dict[str, tuple[str, ...]] = {
    "core": ("src/msp_bonds_mcp/core", "tests"),
    "vtb": ("components/vtb_reports/src/msp_bonds_vtb_reports", "components/vtb_reports/tests"),
    "google": ("components/google_workspace/src/msp_bonds_google", "components/google_workspace/tests"),
    "telegram": ("components/telegram/src/msp_bonds_telegram", "components/telegram/tests"),
    "bond_search": ("components/bond_search/src/msp_bonds_bond_search", "components/bond_search/tests"),
}
_PACKAGE_NAMES = (
    "msp-bonds-mcp", "msp-bonds-vtb-reports", "msp-bonds-google-workspace",
    "msp-bonds-telegram", "msp-bonds-bond-search",
)
_MAX_FILES = 250
_MAX_FILE_SIZE = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024


def _package_versions() -> dict[str, str]:
    versions = {}
    for name in _PACKAGE_NAMES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "NOT_INSTALLED"
        except Exception:
            versions[name] = "UNKNOWN"
    return versions


def _safe_source_path(root: Path, path: Path) -> bool:
    """Reject redirects in every path component, including Windows junctions."""
    try:
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                return False
        return path.resolve(strict=True) == path and path.is_relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return False


def _declared_tools(root: Path, server_path: Path) -> dict:
    """Static source-level tool names; not a runtime MCP registry query."""
    try:
        if not _safe_source_path(root, server_path) or not server_path.is_file() or server_path.stat().st_size > _MAX_FILE_SIZE:
            return {"status": "UNAVAILABLE"}
        tree = ast.parse(server_path.read_text(encoding="utf-8"))
        names = []
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and isinstance(dec.func.value, ast.Name)
                        and dec.func.value.id == "mcp" and dec.func.attr == "tool"):
                    names.append(node.name)
                    break
        return {"status": "OK", "count": len(names), "names": names,
                "meaning": "Declared in installed server.py; compare with MCP client discovery"}
    except Exception:
        return {"status": "UNAVAILABLE"}


def collect_for_root(project_root: Path, component: str = "core") -> dict:
    """Internal implementation. Only the no-path MCP wrapper is exposed."""
    if component != "all" and component not in _SOURCE_GROUPS:
        return {"status": "INVALID_COMPONENT", "allowed": [*_SOURCE_GROUPS, "all"]}
    root = project_root.resolve()
    selected = _SOURCE_GROUPS if component == "all" else {component: _SOURCE_GROUPS[component]}
    result = []
    skipped = {"missing_directories": 0, "unsafe_or_unreadable": 0, "too_large": 0}
    total_bytes = 0
    for group, folders in selected.items():
        for folder in folders:
            directory = root / folder
            if not _safe_source_path(root, directory) or not directory.is_dir():
                skipped["missing_directories"] += 1
                continue
            # Direct *.py children only. Do not recursively read arbitrary directories.
            for path in sorted(directory.glob("*.py"), key=lambda p: p.name.casefold()):
                if len(result) >= _MAX_FILES or total_bytes >= _MAX_TOTAL_BYTES:
                    return {"status": "PARTIAL", "reason": "SOURCE_BUDGET_EXCEEDED",
                            "component": component, "files": result, "skipped": skipped}
                try:
                    if not _safe_source_path(root, path) or not path.is_file():
                        skipped["unsafe_or_unreadable"] += 1
                        continue
                    st = path.stat()
                    if st.st_size > _MAX_FILE_SIZE or total_bytes + st.st_size > _MAX_TOTAL_BYTES:
                        skipped["too_large"] += 1
                        continue
                    # Hash only; never return source bytes or absolute paths.
                    with path.open("rb") as f:
                        digest = hashlib.file_digest(f, "sha256").hexdigest()
                    rel = path.relative_to(root).as_posix()
                    result.append({"path": rel, "sha256": digest, "bytes": st.st_size,
                                   "modified_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                                   "group": group})
                    total_bytes += st.st_size
                except (OSError, ValueError, RuntimeError):
                    skipped["unsafe_or_unreadable"] += 1
    # Also include the root entrypoint filename if it exists, with no contents.
    if component in ("core", "all"):
        path = root / "server.py"
        try:
            if _safe_source_path(root, path) and path.is_file():
                st = path.stat()
                if st.st_size <= _MAX_FILE_SIZE and len(result) < _MAX_FILES and total_bytes + st.st_size <= _MAX_TOTAL_BYTES:
                    with path.open("rb") as f:
                        digest = hashlib.file_digest(f, "sha256").hexdigest()
                    result.append({"path": "server.py", "sha256": digest,
                                   "bytes": st.st_size,
                                   "modified_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                                   "group": "core"})
        except (OSError, ValueError, RuntimeError):
            skipped["unsafe_or_unreadable"] += 1
    result.sort(key=lambda f: f["path"])
    declared = _declared_tools(root, root / "src/msp_bonds_mcp/core/server.py")
    return {
        "status": "OK" if result else "NO_SOURCE_FILES", "component": component,
        "mode": "READ_ONLY", "versions": _package_versions(),
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "files": result, "file_count": len(result), "skipped": skipped,
        "source_declared_mcp_tools": declared,
        "note": "No arbitrary paths, file contents, credentials, environment values or archives are returned",
    }


def collect_diagnostics(component: str = "core") -> dict:
    """MCP call target: fixed installed tree only; no path parameter."""
    project_root = Path(__file__).resolve().parents[3]
    return collect_for_root(project_root, component)
