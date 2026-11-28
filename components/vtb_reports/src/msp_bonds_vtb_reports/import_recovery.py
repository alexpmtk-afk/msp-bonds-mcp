"""Explicit VTB recovery operations. Read cloud evidence, update private metadata only.

Uses the existing queue lock and existing exact-byte recovery function. No upload,
no approval execution, no scheduler, no changes to source documents or write gates.
"""
from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath

from . import vtb_mail_intake as intake
from .gmail_discovery import load_archive_registry
from .vtb_registry_refresh import _drive_exact_file, refresh_vtb_registry
from msp_bonds_google import workspace as gw
from msp_bonds_google.identity import assert_same_account
from msp_bonds_google.writes import _readback


def _answer(status, **details):
    return {"status": status, "cloud_writes": 0,
            "local_approval_required_for_upload": True, **details}


def get_import_status():
    path = intake._runtime_path()
    # Atomic queue replacement permits a consistent read without a writer lock.
    if path.with_name(path.name + '.tmp').exists():
        return _answer("NEEDS_REVIEW", reason="QUEUE_TEMP_FILE_PRESENT")
    state = intake._read_state(path)
    return _answer("OK", queue_exists=path.exists(),
                   queue_counts=dict(Counter(i['state'] for i in state['items'])),
                   items=[{"sha256_prefix": i['sha256'][:12], "state": i['state']}
                          for i in state['items'] if intake._valid_digest(i.get('sha256'))],
                   total=len(state['items']))


def _confirmed(item, records):
    """Require exact digest, destination, remote ID and basename, not digest alone."""
    digest, folder, file_id, name = (item.get(k) for k in
        ('sha256', 'target_folder_id', 'drive_file_id', 'filename'))
    if not intake._valid_digest(digest) or not all(isinstance(x, str) and x for x in (folder, file_id, name)):
        return False
    matches = [r for r in records if r.get('drive_verification') == 'MATCH'
               and r.get('drive_file_id') == file_id]
    return len(matches) == 1 and all((
        matches[0].get('sha256') == digest,
        matches[0].get('drive_parent_folder_id') == folder,
        PurePosixPath(str(matches[0].get('relative_path', '')).replace('\\', '/')).name == name,
        matches[0].get('size_bytes') == item.get('size_bytes'),
    ))


def reconcile_import(sha256_prefix: str):
    """Recover one uncertain or pending-sync item; never authorize a re-upload."""
    path = intake._runtime_path()
    with intake._locked_queue(path):
        state = intake._read_state(path)
        item = intake._select(state, sha256_prefix)
        if item['state'] not in {'UPLOAD_UNCERTAIN', 'UPLOADED_PENDING_SYNC'}:
            return _answer('NOT_RECOVERABLE', queue_state=item['state'])
        records, complete, _ = load_archive_registry(intake._registry_path())
        if not complete:
            return _answer('BLOCKED', reason='ARCHIVE_REGISTRY_UNVERIFIED', queue_state=item['state'])
        assert_same_account()
        if item['state'] == 'UPLOAD_UNCERTAIN':
            result = intake.reconcile_uncertain_item(item, stage_root=gw.staging_dir(),
                archive_records=records, lookup=_drive_exact_file, readback=_readback)
            if result != 'UPLOADED_PENDING_SYNC':
                return _answer(result, queue_state=item['state'])
            # Durable proof of remote upload precedes any registry refresh.
            intake._write_state(path, state)
        if not intake._valid_digest(item.get('sha256')) or not item.get('drive_file_id') or not item.get('filename') or item.get('target_folder_id') not in intake._trusted_folders(records):
            return _answer('BLOCKED', reason='INVALID_PENDING_EVIDENCE', queue_state=item['state'])
        try:
            result = refresh_vtb_registry(target_parent_id=item['target_folder_id'],
                        expected_name=item['filename'], expected_file_id=item['drive_file_id'],
                        expected_sha256=item['sha256'], registry_path=intake._registry_path())
        except Exception:
            return _answer('REGISTRY_CHECK_FAILED', queue_state=item['state'])
        status = result.get('status', 'ERROR')
        if status not in {'UPDATED', 'UP_TO_DATE'}:
            return _answer(status, queue_state=item['state'])
        records, complete, _ = load_archive_registry(intake._registry_path())
        if not complete or not _confirmed(item, records):
            return _answer('REGISTRY_EVIDENCE_NOT_CONFIRMED', queue_state=item['state'])
        item['state'] = 'ALREADY_ARCHIVED'
        item.pop('reason', None)
        intake._write_state(path, state)
        return _answer('ALREADY_ARCHIVED', queue_state=item['state'], registry_status=status)


def refresh_archive_registry():
    """Explicit registry refresh, protected by the existing intake queue lock."""
    with intake._locked_queue(intake._runtime_path()):
        assert_same_account()
        result = refresh_vtb_registry(registry_path=intake._registry_path())
        return _answer(result.get('status', 'ERROR'), **{k: result[k] for k in
                       ('total', 'matched', 'added', 'pending_files') if k in result})
