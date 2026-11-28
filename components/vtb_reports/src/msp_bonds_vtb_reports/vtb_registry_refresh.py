"""Safely refresh VTB archive registry after Drive sync, using verified metadata.

Only private metadata is modified. Never writes original reports or Google Drive.
Old verified records are rechecked against local SHA-256; new records require
Drive metadata MD5 and size to agree with the local document.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from msp_bonds_mcp.core.paths import runtime_dir
from msp_bonds_google import workspace as gw
from msp_bonds_google.utils import drive_q_literal

from .archive_reconciliation import _walk_files, reconcile_archive, write_archive_registry
from .gmail_discovery import load_archive_registry


def _normal_relative(value: str) -> str:
    return PurePosixPath(str(value).replace('\\', '/')).as_posix().casefold()


def _drive_exact_file(parent_id: str, name: str) -> dict[str, Any] | None:
    """Return metadata for exactly one remote name; duplicates are not safe."""
    q = (f"'{drive_q_literal(parent_id)}' in parents and "
         f"name = '{drive_q_literal(name)}' and trashed = false")
    token = ''
    matches: list[dict[str, Any]] = []
    for _ in range(4):
        params = {
            'q': q,
            'fields': 'nextPageToken,files(id,name,size,md5Checksum,parents,mimeType)',
            'pageSize': 100,
            'supportsAllDrives': True,
            'includeItemsFromAllDrives': True,
        }
        if token:
            params['pageToken'] = token
        reply = gw._drive().files().list(**params).execute()
        matches.extend(x for x in reply.get('files', []) if x.get('name') == name)
        token = str(reply.get('nextPageToken') or '')
        if not token:
            break
    if token:
        raise ValueError('REMOTE_LIST_PAGINATION_LIMIT')
    if len(matches) > 1:
        raise ValueError('REMOTE_FILENAME_AMBIGUOUS')
    return matches[0] if matches else None


def refresh_vtb_registry(
    *,
    target_parent_id: str = '',
    expected_name: str = '',
    expected_file_id: str = '',
    expected_sha256: str = '',
    root: str | Path | None = None,
    registry_path: str | Path | None = None,
) -> dict[str, Any]:
    """Reconcile X: with already verified registry and Drive, fail closed.

    The optional expected_* identifiers are supplied by the approved uploader.
    File sync to X: is asynchronous: DEFERRED_LOCAL_SYNC is not a failure and
    must not be reported as ALREADY_ARCHIVED. A scheduled retry is safe.
    """
    archive_root_value = (root if root is not None else
                          (os.environ.get('MSP_BONDS_REPORTS_ROOT') or
                           r'<LOCAL_PATH_REDACTED> Биржа Облигации\Отчёты ВТБ'))
    if not str(archive_root_value).strip():
        return {'status': 'NOT_CONFIGURED'}
    archive_root = Path(archive_root_value)
    p = Path(registry_path) if registry_path is not None else runtime_dir() / 'vtb_archive_registry.json'
    previous, complete, reason = load_archive_registry(p)
    if not complete:
        return {'status': 'REGISTRY_UNVERIFIED', 'reason': reason}
    if not archive_root.is_dir():
        return {'status': 'DEFERRED_LOCAL_SYNC', 'reason': 'ARCHIVE_ROOT_NOT_AVAILABLE'}

    confirmed_folders: set[str] = set()
    folder_for_parent: dict[str, str] = {}
    old_by_rel: dict[str, dict[str, Any]] = {}
    remote: dict[str, dict[str, Any]] = {}
    for row in previous:
        rel = str(row['relative_path'])
        parent = str(row['drive_parent_folder_id'])
        parent_rel = _normal_relative(str(PurePosixPath(rel).parent))
        if parent_rel in folder_for_parent and folder_for_parent[parent_rel] != parent:
            return {'status': 'NEEDS_REVIEW', 'reason': 'CONFLICTING_VERIFIED_FOLDERS'}
        folder_for_parent[parent_rel] = parent
        confirmed_folders.add(parent)
        key = _normal_relative(rel)
        if key in old_by_rel:
            return {'status': 'NEEDS_REVIEW', 'reason': 'DUPLICATE_REGISTRY_PATH'}
        old_by_rel[key] = row
        remote[rel] = {
            'id': row['drive_file_id'],
            'size': row['size_bytes'],
            'sha256': row['sha256'],
            'parent_id': parent,
        }

    if target_parent_id and target_parent_id not in confirmed_folders:
        return {'status': 'SKIPPED_NON_VTB_FOLDER'}

    found = set()
    added: list[str] = []
    pending: list[str] = []
    expected_local = False
    for category, path in _walk_files(archive_root):
        rel = path.relative_to(archive_root).as_posix()
        key = _normal_relative(rel)
        found.add(key)
        parent = folder_for_parent.get(_normal_relative(str(PurePosixPath(rel).parent)))
        if key in old_by_rel:
            if target_parent_id and parent == target_parent_id and path.name == expected_name:
                expected_local = True
                if expected_file_id and old_by_rel[key]['drive_file_id'] != expected_file_id:
                    return {'status': 'NEEDS_REVIEW', 'reason': 'EXPECTED_FILE_ID_CONFLICT'}
                if expected_sha256 and old_by_rel[key]['sha256'] != expected_sha256:
                    return {'status': 'NEEDS_REVIEW', 'reason': 'EXPECTED_SHA256_CONFLICT'}
            continue
        if parent is None:
            return {'status': 'NEEDS_REVIEW', 'reason': 'UNKNOWN_LOCAL_ARCHIVE_FOLDER'}
        if target_parent_id and parent == target_parent_id and path.name == expected_name:
            expected_local = True
        try:
            meta = _drive_exact_file(parent, path.name)
        except ValueError as exc:
            return {'status': 'NEEDS_REVIEW', 'reason': str(exc)}
        if not meta:
            pending.append(rel)
            continue
        if not meta.get('id') or not meta.get('md5Checksum') or meta.get('size') is None:
            return {'status': 'NEEDS_REVIEW', 'reason': 'REMOTE_MD5_OR_SIZE_MISSING'}
        if (meta.get('parents') or []) != [parent]:
            return {'status': 'NEEDS_REVIEW', 'reason': 'REMOTE_PARENT_UNVERIFIED'}
        if target_parent_id and parent == target_parent_id and path.name == expected_name:
            if expected_file_id and expected_file_id != meta['id']:
                return {'status': 'NEEDS_REVIEW', 'reason': 'EXPECTED_FILE_ID_CONFLICT'}
        remote[rel] = meta
        added.append(rel)

    if not set(old_by_rel).issubset(found):
        return {'status': 'NEEDS_REVIEW', 'reason': 'PREVIOUS_ARCHIVE_FILE_MISSING'}
    if pending:
        return {'status': 'DEFERRED_REMOTE_METADATA', 'pending_files': len(pending)}
    if len(found) > 10000:
        return {'status': 'NEEDS_REVIEW', 'reason': 'ARCHIVE_SCAN_LIMIT'}

    # Recompute all local SHA-256 values and check every record before committing.
    new_registry = reconcile_archive(archive_root, remote)
    if (new_registry['status'] != 'OK' or new_registry['total'] != len(found)
            or new_registry['drive_verification_counts']['MATCH'] != len(found)
            or new_registry['classification_counts']['FAIL']):
        return {'status': 'NEEDS_REVIEW', 'reason': 'REGISTRY_RECONCILIATION_FAILED',
                'total_found': len(found)}

    added_keys = {_normal_relative(rel) for rel in added}
    if any(_normal_relative(row['relative_path']) in added_keys
           and row['classification_outcome'] != 'PASS'
           for row in new_registry['records']):
        return {'status': 'NEEDS_REVIEW', 'reason': 'NEW_DOCUMENT_CLASSIFICATION_NOT_CONFIRMED'}

    if expected_local and expected_sha256:
        matching = [r for r in new_registry['records']
                    if r['sha256'] == expected_sha256 and r['drive_file_id'] == expected_file_id
                    and r['drive_parent_folder_id'] == target_parent_id]
        if len(matching) != 1:
            return {'status': 'NEEDS_REVIEW', 'reason': 'EXPECTED_DOCUMENT_NOT_VERIFIED'}

    updated = bool(added or len(found) != len(previous))
    if updated:
        backup = p.with_name(p.name + '.bak_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        shutil.copy2(p, backup)
        write_archive_registry(p, new_registry)

    if target_parent_id and expected_name and not expected_local:
        status = 'DEFERRED_LOCAL_SYNC'
    else:
        status = 'UPDATED' if updated else 'UP_TO_DATE'
    return {'status': status, 'total': new_registry['total'],
            'matched': new_registry['drive_verification_counts']['MATCH'],
            'added': len(added)}


def main() -> int:
    try:
        result = refresh_vtb_registry()
    except Exception as exc:
        # No provider exception or financial document body is printed.
        result = {'status': 'ERROR', 'error_type': type(exc).__name__}
    print('VTB_REGISTRY=' + result['status'] + ' TOTAL=' + str(result.get('total', '?'))
          + ' ADDED=' + str(result.get('added', 0)))
    return 0 if result['status'] in ('UPDATED', 'UP_TO_DATE', 'DEFERRED_LOCAL_SYNC', 'NOT_CONFIGURED') else 1


if __name__ == '__main__':
    raise SystemExit(main())
