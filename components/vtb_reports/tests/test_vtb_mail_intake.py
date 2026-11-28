"""Isolated tests: no Gmail, no Drive mutation, no financial documents."""
from __future__ import annotations
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from msp_bonds_vtb_reports.vtb_mail_intake import (
    _read_state, _select, _staged_valid, _write_state, merge_scan_into_queue,
)

DATA = b'virtual-test-bytes-do-not-upload'
SHA = hashlib.sha256(DATA).hexdigest()
FOLDER = 'folder-confirmed'


def old_rows():
    return [{"sha256": 'a' * 64, "drive_verification": "MATCH", "drive_parent_folder_id": FOLDER}]


def new_result(digest=SHA, folder=FOLDER, filename='test-report.pdf'):
    return {"status": "NEW", "sha256": digest, "target_folder_id": folder,
            "gmail_message_id": 'gmail-id-test', "part_id": '1', "filename": filename,
            "size_bytes": len(DATA), "category": "Брокерский отчёт", "account_ids": ['TEST01']}


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.stage = self.root / 'staging'
        self.stage.mkdir()
        self.calls = 0
        self.state = {"schema": 1, "items": [], "scanned_at": "", "last_scan": {}}

    def fake_stage(self, message, part):
        self.calls += 1
        name = f'{self.calls}_test-report.pdf'
        (self.stage / name).write_bytes(DATA)
        return {"staged_name": name, "suggested_name": 'test-report.pdf',
                "sha256": SHA, "size": len(DATA)}

    def merge(self, results, rows=None):
        return merge_scan_into_queue(state=self.state,
            scan={"status": "OK", "archive_registry_complete": True, "results": results},
            archive_records=old_rows() if rows is None else rows,
            stage=self.fake_stage, stage_root=self.stage)

    def test_new_valid_stages_once(self):
        r = self.merge([new_result()]);self.assertEqual(r['staged'], 1)
        self.assertEqual(self.state['items'][0]['state'], 'PENDING_APPROVAL')
        self.assertTrue(_staged_valid(self.state['items'][0], self.stage))
        self.assertEqual(self.calls, 1)

    def test_repeat_scan_not_downloaded_again(self):
        self.merge([new_result()]);r = self.merge([new_result()])
        self.assertEqual(r['previously_queued'], 1);self.assertEqual(self.calls, 1)

    def test_ignored_unconfirmed_destination(self):
        r = self.merge([new_result(folder='untrusted-folder')])
        self.assertEqual(r['problems'], 1);self.assertEqual(self.calls, 0)

    def test_invalid_sha_rejected(self):
        r = self.merge([new_result(digest='nothex')]);self.assertEqual(r['problems'], 1)

    def test_archive_hash_skips_staging(self):
        r = self.merge([new_result()], rows=old_rows()+[{"sha256":SHA,"drive_verification":"MATCH","drive_parent_folder_id":FOLDER}])
        self.assertEqual(r['already_archived'],1);self.assertEqual(self.calls, 0)

    def test_stale_queued_file_restaged(self):
        self.merge([new_result()]);(self.stage / self.state['items'][0]['staged_name']).unlink()
        r=self.merge([new_result()]);self.assertEqual(r['staged'],1);self.assertEqual(self.calls,2)

    def test_staged_file_modified_detected(self):
        self.merge([new_result()]);(self.stage / self.state['items'][0]['staged_name']).write_bytes(b'wrong')
        self.assertFalse(_staged_valid(self.state['items'][0],self.stage))

    def test_uploaded_is_never_restaged(self):
        self.merge([new_result()]);self.state['items'][0]['state']='UPLOADED_PENDING_SYNC'
        r=self.merge([new_result()]);self.assertEqual(r['previously_queued'],1);self.assertEqual(self.calls,1)

    def test_archive_registry_changes_state(self):
        self.merge([new_result()]);rows=old_rows()+[{"sha256":SHA,"drive_verification":"MATCH","drive_parent_folder_id":FOLDER}]
        self.merge([],rows=rows);self.assertEqual(self.state['items'][0]['state'],'ALREADY_ARCHIVED')

    def test_conflicting_route_marks_review(self):
        self.merge([new_result()]);rows=old_rows()+[{"sha256":'b'*64,"drive_verification":"MATCH","drive_parent_folder_id":"other-folder"}]
        r=self.merge([new_result(folder='other-folder')], rows=rows)
        self.assertEqual(r['problems'],1);self.assertEqual(self.state['items'][0]['state'],'NEEDS_REVIEW')

    def test_queue_json_roundtrip(self):
        path=self.root/'q.json';_write_state(path,self.state)
        self.assertEqual(_read_state(path),self.state)
        self.assertFalse(path.with_name('q.json.tmp').exists())

    def test_invalid_queue_rejected(self):
        path=self.root/'q.json';path.write_text('{}',encoding='utf-8')
        with self.assertRaises(ValueError):_read_state(path)

    def test_ambiguous_hash_selector(self):
        self.merge([new_result()]);self.assertEqual(_select(self.state,SHA[:12])['sha256'],SHA)
        with self.assertRaises(ValueError):_select(self.state,SHA[:10])

    def test_scan_registry_unverified_fails_closed(self):
        with self.assertRaises(ValueError):merge_scan_into_queue(
            state=self.state,scan={'status':'OK','archive_registry_complete':False,'results':[new_result()]},
            archive_records=old_rows(),stage=self.fake_stage,stage_root=self.stage)

    def test_stage_hash_mismatch_not_added(self):
        def wrong(*a):return {"staged_name":"wrong.pdf","sha256":"a"*64,"size":len(DATA),"suggested_name":"test-report.pdf"}
        r=merge_scan_into_queue(state=self.state, scan={'status':'OK','archive_registry_complete':True,'results':[new_result()]},
            archive_records=old_rows(),stage=wrong,stage_root=self.stage)
        self.assertEqual(r['problems'],1);self.assertEqual(self.state['items'],[])

class VersionConflictTests(unittest.TestCase):
    def test_depot_same_date_different_hash_blocked(self):
        from msp_bonds_vtb_reports.vtb_mail_intake import _conflict_with_verified_version
        from msp_bonds_vtb_reports.classifier import ClassificationResult
        c=ClassificationResult('Выписка по счёту депо',('B12345','shared','20.11.2026'),None,None,None,None,SHA,0.9,'CLASSIFIED',())
        rows=[{'drive_verification':'MATCH','detected_category':c.category,'drive_parent_folder_id':'f1',
               'sha256':h,'account_ids':['B12345','shared'],
               'relative_path':name} for h,name in [('1'*64,'Выписка по счёту депо/БР/за 13.11.2026.xlsx'),('2'*64,'Выписка по счёту депо/БР/за 06.11.2026.xlsx')]]
        rows.append({'drive_verification':'MATCH','detected_category':c.category,'drive_parent_folder_id':'f2',
                     'sha256':'3'*64,'account_ids':['shared'], 'relative_path':'ИИС/за 12.11.2026.xlsx'})
        self.assertTrue(_conflict_with_verified_version(c,'Выписка депо за 13.11.2026.xlsx','f1',rows))
        self.assertFalse(_conflict_with_verified_version(c,'Выписка депо за 15.11.2026.xlsx','f1',rows))
        with self.assertRaises(ValueError):_conflict_with_verified_version(c,'Выписка без даты.xlsx','f1',rows)

    def test_payment_same_period_blocked(self):
        from msp_bonds_vtb_reports.vtb_mail_intake import _conflict_with_verified_version
        from msp_bonds_vtb_reports.classifier import ClassificationResult
        c=ClassificationResult('Отчёт о выплатах по ценным бумагам',('B12345',),'2026-11-06','2026-11-13',None,None,SHA,0.99,'CLASSIFIED',())
        rows=[{'drive_verification':'MATCH','detected_category':c.category,'drive_parent_folder_id':'f1',
               'sha256':h,'account_ids':['B12345'], 'period_start':a, 'period_end':b} for h,a,b in [
                   ('1'*64,'2026-11-06','2026-11-13'),('2'*64,'2026-10-01','2026-11-06')]]
        self.assertTrue(_conflict_with_verified_version(c,'test.pdf','f1',rows))
        c2=ClassificationResult(c.category,c.account_ids,'2026-11-13','2026-11-20',None,None,SHA,0.99,'CLASSIFIED',())
        self.assertFalse(_conflict_with_verified_version(c2,'test.pdf','f1',rows))

if __name__ == "__main__":
    unittest.main()
