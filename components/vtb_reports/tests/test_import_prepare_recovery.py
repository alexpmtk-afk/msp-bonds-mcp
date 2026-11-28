"""Offline synthetic tests for bounded preparation and uncertain upload recovery."""
from __future__ import annotations
import hashlib
import io
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile
import unittest
from types import SimpleNamespace

from msp_bonds_vtb_reports.gmail_discovery import scan_gmail_reports
from msp_bonds_vtb_reports.import_prepare import prepare_import_page
from msp_bonds_vtb_reports.vtb_mail_intake import (
    _read_state, _write_state, reconcile_uncertain_item, merge_scan_into_queue,
)

FOLDER = 'synthetic-folder'
DATA = b'synthetic document bytes for queue/recovery tests'
SHA = hashlib.sha256(DATA).hexdigest()
ROWS = [{'sha256': 'a' * 64, 'drive_verification': 'MATCH', 'drive_parent_folder_id': FOLDER}]


def workbook(text):
    out = io.BytesIO()
    with ZipFile(out, 'w') as z:
        z.writestr('[Content_Types].xml', '<Types/>')
        z.writestr('xl/workbook.xml', '<workbook/>')
        z.writestr('xl/worksheets/sheet1.xml', '<worksheet><sheetData><row><c t="inlineStr"><is><t>' + text + '</t></is></c></row></sheetData></worksheet>')
    return out.getvalue()


def scan_fixture(data, *, page=None, token='', rows=()):
    return scan_gmail_reports(
        search_page=page or (lambda **_: {'messages': [{'id': 'synthetic-message'}]}),
        get_message=lambda _: {'labels': ['broker'], 'headers': {'from': 'test@example.invalid'},
                               'attachments': [{'part_id': '1', 'attachment_id': 'synthetic', 'filename': 'test.xlsx',
                                                'size': len(data), 'mime_type': 'application/octet-stream'}]},
        get_attachment=lambda *_: data, archive_records=rows, archive_complete=True,
        allowed_senders={'test@example.invalid'}, broker_label_id='broker', corporate_action_label_id='notice',
        page_token=token, page_size=1, max_pages=1)










def candidate():
    return {'status': 'NEW', 'sha256': SHA, 'target_folder_id': FOLDER, 'gmail_message_id': 'synthetic-message',
            'part_id': '1', 'filename': 'test.pdf', 'size_bytes': len(DATA),
            'category': 'Брокерский отчёт', 'account_ids': ['SYNTHETIC001']}








def uncertain(tmp_path):
    (tmp_path/'staged.pdf').write_bytes(DATA)
    return {**candidate(), 'state': 'UPLOAD_UNCERTAIN', 'staged_name': 'staged.pdf'}


def remote():
    return {'id': 'synthetic-drive-id', 'name': 'test.pdf', 'parents': [FOLDER], 'size': str(len(DATA))}

class ImportPrepareRecoveryTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.tmp_path = Path(work.name)

    def test_explicit_corporate_notification_is_counted_separately(self):
        result = scan_fixture(workbook('Уведомление о корпоративном действии. Купон.'))
        assert result['status'] == 'OK'
        assert result['attachments_checked'] == 1
        assert result['skipped_notification_attachments'] == 1
        assert sum(result['result_counts'].values()) == 0


    def test_ambiguous_report_titles_still_require_review(self):
        result = scan_fixture(workbook('Брокерский отчёт. Выписка по счету депо. Уведомление о купоне.'))
        assert result['skipped_notification_attachments'] == 0
        assert result['result_counts']['NEEDS_REVIEW'] == 1
        assert result['reason_counts']['AMBIGUOUS_CATEGORY_TITLES'] == 1


    def test_archived_exact_hash_retains_priority_over_notification_skip(self):
        data = workbook('Уведомление о корпоративном действии. Купон.')
        result = scan_fixture(data, rows=[{'sha256': hashlib.sha256(data).hexdigest(), 'drive_verification': 'MATCH', 'drive_file_id': 'test-file', 'drive_parent_folder_id': FOLDER}])
        assert result['result_counts']['ALREADY_ARCHIVED'] == 1
        assert result['skipped_notification_attachments'] == 0


    def test_search_failure_preserves_retry_token_without_claiming_success(self):
        failure = Mock(side_effect=TimeoutError())
        result = scan_fixture(b'', page=failure, token='retry-me')
        assert result['status'] == 'ERROR'
        assert result['next_page_token'] == ''
        assert result['retry_page_token'] == 'retry-me'
        assert result['errors'][0]['reason_code'] == 'GMAIL_SEARCH_FAILED'


    def test_prepare_partial_page_and_repeat_is_idempotent(self):
        tmp_path = self.tmp_path
        stage_root = tmp_path / 'stage'; stage_root.mkdir()
        def stage(*_):
            (stage_root / 'staged.pdf').write_bytes(DATA)
            return {'staged_name': 'staged.pdf', 'suggested_name': 'test.pdf', 'size': len(DATA), 'sha256': SHA}
        stage_mock = Mock(side_effect=stage)
        scan = {'status': 'PARTIAL', 'archive_registry_complete': True, 'results': [candidate()], 'next_page_token': 'next'}
        kwargs = dict(queue_path=tmp_path / 'queue.json', stage_root=stage_root, scan_page=lambda: scan,
                      load_registry=lambda: (ROWS, True, ''), stage=stage_mock)
        first = prepare_import_page(**kwargs); second = prepare_import_page(**kwargs)
        assert first['status'] == 'PREPARED_PARTIAL'
        assert first['cloud_writes'] == 0 and first['local_approval_required'] is True
        assert first['next_page_token'] == 'next'
        assert first['staged'] == 1 and second['previously_queued'] == 1
        assert second['queue_counts'] == {'PENDING_APPROVAL': 1}
        assert stage_mock.call_count == 1


    def test_prepare_fails_closed_on_registry_change(self):
        tmp_path = self.tmp_path
        stage = Mock(); scan = Mock(return_value={'status': 'OK', 'archive_registry_complete': True, 'results': [candidate()]})
        registry = Mock(side_effect=[(ROWS, True, ''), (ROWS + [{'extra': True}], True, '')])
        result = prepare_import_page(queue_path=tmp_path/'queue.json', stage_root=tmp_path,
                                     scan_page=scan, load_registry=registry, stage=stage)
        assert result['reason'] == 'ARCHIVE_REGISTRY_CHANGED_DURING_SCAN'
        stage.assert_not_called()
        assert not (tmp_path/'queue.json').exists()


    def test_prepare_unverified_archive_never_scans_or_stages(self):
        tmp_path = self.tmp_path
        scan = Mock(); stage = Mock()
        r = prepare_import_page(queue_path=tmp_path/'queue.json', stage_root=tmp_path,
                                scan_page=scan, load_registry=lambda: ([], False, ''), stage=stage)
        assert r['status'] == 'BLOCKED'; scan.assert_not_called(); stage.assert_not_called()


    def test_recovery_verified_remote_is_not_uploaded_again(self):
        tmp_path = self.tmp_path
        item = uncertain(tmp_path); readback = Mock(return_value={'sha256_verified': True, 'sha256': SHA})
        status = reconcile_uncertain_item(item, stage_root=tmp_path, archive_records=ROWS, lookup=lambda *_: remote(), readback=readback)
        assert status == 'UPLOADED_PENDING_SYNC'
        assert item['drive_file_id'] == 'synthetic-drive-id'
        readback.assert_called_once_with('synthetic-drive-id', DATA)


    def test_uncertain_remote_result_never_enables_retry(self):
        tmp_path = self.tmp_path
        cases = [
            (None, {}, 'UNCERTAIN_NOT_FOUND_NO_RETRY'),
            ({**remote(), 'parents': ['other']}, {}, 'UNCERTAIN_REMOTE_IDENTITY_MISMATCH'),
            ({**remote(), 'size': '1'}, {}, 'UNCERTAIN_REMOTE_IDENTITY_MISMATCH'),
            (remote(), {'sha256_verified': False, 'sha256': SHA}, 'UNCERTAIN_REMOTE_HASH_MISMATCH'),
            (remote(), {'sha256_verified': True, 'sha256': '0'*64}, 'UNCERTAIN_REMOTE_HASH_MISMATCH'),
        ]
        for meta, answer, expected in cases:
            item = uncertain(tmp_path); readback = Mock(return_value=answer)
            status = reconcile_uncertain_item(item, stage_root=tmp_path, archive_records=ROWS, lookup=lambda *_: meta, readback=readback)
            assert status == expected
            assert item['state'] == 'UPLOAD_UNCERTAIN'
            if expected != 'UNCERTAIN_REMOTE_HASH_MISMATCH': readback.assert_not_called()


    def test_uncertain_lookup_failure_keeps_state(self):
        tmp_path = self.tmp_path
        item = uncertain(tmp_path); readback = Mock()
        r = reconcile_uncertain_item(item, stage_root=tmp_path, archive_records=ROWS,
                                     lookup=Mock(side_effect=TimeoutError()), readback=readback)
        assert r == 'UNCERTAIN_REMOTE_CHECK_FAILED' and item['state'] == 'UPLOAD_UNCERTAIN'
        readback.assert_not_called()


    def test_corrupted_staging_does_not_read_remote(self):
        tmp_path = self.tmp_path
        item = uncertain(tmp_path); (tmp_path/'staged.pdf').write_bytes(b'altered')
        lookup = Mock()
        r = reconcile_uncertain_item(item, stage_root=tmp_path, archive_records=ROWS, lookup=lookup, readback=Mock())
        assert r == 'UNCERTAIN_LOCAL_EVIDENCE_INVALID'; lookup.assert_not_called()


    def test_repeat_scan_cannot_restage_uncertain_upload(self):
        tmp_path = self.tmp_path
        item = uncertain(tmp_path); state = {'schema': 1, 'items': [item]}
        stage = Mock()
        r = merge_scan_into_queue(state=state, scan={'status': 'OK', 'archive_registry_complete': True, 'results': [candidate()]},
                                  archive_records=ROWS, stage=stage, stage_root=tmp_path)
        assert r['previously_queued'] == 1; stage.assert_not_called()
        assert item['state'] == 'UPLOAD_UNCERTAIN'
        path = tmp_path/'queue.json'; _write_state(path, state)
        assert _read_state(path)['items'][0]['state'] == 'UPLOAD_UNCERTAIN'


    def test_approval_persists_uncertainty_before_network_failure(self):
        from msp_bonds_vtb_reports import vtb_mail_intake as intake
        from contextlib import ExitStack
        tmp_path = self.tmp_path
        item = uncertain(tmp_path); item['state'] = 'PENDING_APPROVAL'
        path = tmp_path/'queue.json'; _write_state(path, {'schema': 1, 'items': [item]})
        classification = SimpleNamespace(status='CLASSIFIED', category=item['category'], account_ids=item['account_ids'])
        def fail_after_intent(_plan_id):
            self.assertEqual(_read_state(path)['items'][0]['state'], 'UPLOAD_UNCERTAIN')
            raise TimeoutError('synthetic crash')
        patches = {
            'msp_bonds_vtb_reports.vtb_mail_intake.sys.stdin.isatty': True,
            'msp_bonds_vtb_reports.vtb_mail_intake._runtime_path': path,
            'msp_bonds_vtb_reports.vtb_mail_intake.load_archive_registry': (ROWS, True, ''),
            'msp_bonds_vtb_reports.vtb_mail_intake.extract_document_text': SimpleNamespace(status='OK', text='synthetic', detected_format='PDF'),
            'msp_bonds_vtb_reports.vtb_mail_intake.classify_document': classification,
            'msp_bonds_vtb_reports.vtb_mail_intake.resolve_target_folder': {'status': 'READY', 'target_folder_id': FOLDER},
            'msp_bonds_vtb_reports.vtb_mail_intake.match_archive_candidate': {'status': 'NEW_DOCUMENT'},
            'msp_bonds_vtb_reports.vtb_mail_intake._conflict_with_verified_version': False,
            'msp_bonds_vtb_reports.gmail_discovery._sender_status': (True, ''),
            'msp_bonds_google.workspace.staging_dir': tmp_path,
            'msp_bonds_google.workspace.gmail_message': {'headers': {}, 'attachments': [{'part_id': '1', 'filename': 'test.pdf'}]},
            'msp_bonds_google.approvals.propose': {'plan_id': 'synthetic-plan'},
            'msp_bonds_google.approvals.approve_interactively': {'approved': True},
        }
        with ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(patch(name, return_value=value))
            execute = stack.enter_context(patch('msp_bonds_google.writes.execute', side_effect=fail_after_intent))
            with self.assertRaises(TimeoutError):
                intake.approve_command(SHA)
            with self.assertRaisesRegex(ValueError, 'NOT_PENDING_APPROVAL'):
                intake.approve_command(SHA)
            execute.assert_called_once()
        self.assertEqual(_read_state(path)['items'][0]['state'], 'UPLOAD_UNCERTAIN')
