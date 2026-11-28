"""Synthetic tests: no credentials, no live cloud calls or original reports."""
import hashlib
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch, Mock
from msp_bonds_vtb_reports import import_recovery as r
from msp_bonds_vtb_reports import vtb_mail_intake as i

DATA = b'synthetic test bytes'
SHA = hashlib.sha256(DATA).hexdigest()
FOLDER = 'synthetic-folder'
ROW = {'drive_verification':'MATCH', 'drive_file_id':'existing', 'drive_parent_folder_id':FOLDER, 'sha256':'a'*64}
MATCH = {'drive_verification':'MATCH', 'drive_file_id':'remote', 'drive_parent_folder_id':FOLDER,
         'sha256':SHA, 'relative_path':'folder/example.pdf', 'size_bytes':len(DATA)}

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.queue = self.root/'queue.json'
        (self.root/'staged.pdf').write_bytes(DATA)
        self.item = {'state':'UPLOAD_UNCERTAIN','sha256':SHA,'filename':'example.pdf',
                     'size_bytes':len(DATA),'target_folder_id':FOLDER,'staged_name':'staged.pdf'}
        self.save()
        for obj,name,val in [(i,'_runtime_path',self.queue),(i,'_registry_path',self.root/'registry.json'),(r.gw,'staging_dir',self.root)]:
            self.stack.enter_context(patch.object(obj,name,return_value=val))
        self.load = self.stack.enter_context(patch.object(r,'load_archive_registry',return_value=([ROW],True,'')))
        self.identity = self.stack.enter_context(patch.object(r,'assert_same_account'))
        self.lookup = self.stack.enter_context(patch.object(r,'_drive_exact_file',return_value={'id':'remote','name':'example.pdf','parents':[FOLDER],'size':len(DATA)}))
        self.read = self.stack.enter_context(patch.object(r,'_readback',return_value={'sha256_verified':True,'sha256':SHA}))
        self.refresh = self.stack.enter_context(patch.object(r,'refresh_vtb_registry',return_value={'status':'DEFERRED_LOCAL_SYNC'}))
    def save(self): i._write_state(self.queue, {'schema':1,'items':[self.item]})
    def state(self): return i._read_state(self.queue)['items'][0]['state']
    def pending(self): self.item.update(state='UPLOADED_PENDING_SYNC',drive_file_id='remote'); self.save()
    def call(self): return r.reconcile_import(SHA[:12])
    def test_status_no_cloud(self):
        result=r.get_import_status(); self.assertEqual(result['queue_counts'],{'UPLOAD_UNCERTAIN':1}); self.assertEqual(result['cloud_writes'],0)
        self.identity.assert_not_called(); self.lookup.assert_not_called(); self.refresh.assert_not_called()
        self.assertNotIn('filename',result['items'][0]); self.assertNotIn('target_folder_id',result['items'][0])
    def test_absent_queue_distinguished(self):
        self.queue.unlink(); self.assertFalse(r.get_import_status()['queue_exists']); self.assertFalse(self.queue.exists())
    def test_temp_file_blocks_status(self):
        self.queue.with_name('queue.json.tmp').touch(); self.assertEqual(r.get_import_status()['status'],'NEEDS_REVIEW')
    def test_pending_approval_never_recovered(self):
        self.item['state']='PENDING_APPROVAL'; self.save(); self.assertEqual(self.call()['status'],'NOT_RECOVERABLE'); self.identity.assert_not_called()
    def test_unverified_registry_blocks(self):
        self.load.return_value=([],False,''); self.assertEqual(self.call()['status'],'BLOCKED'); self.lookup.assert_not_called()
    def test_missing_remote_no_retry(self):
        self.lookup.return_value=None; self.assertEqual(self.call()['status'],'UNCERTAIN_NOT_FOUND_NO_RETRY'); self.assertEqual(self.state(),'UPLOAD_UNCERTAIN'); self.refresh.assert_not_called()
    def test_hash_mismatch(self):
        self.read.return_value={'sha256_verified':False,'sha256':SHA}; self.assertEqual(self.call()['status'],'UNCERTAIN_REMOTE_HASH_MISMATCH'); self.assertEqual(self.state(),'UPLOAD_UNCERTAIN')
    def test_verified_upload_persisted_before_refresh(self):
        def check(**kwargs): self.assertEqual(self.state(),'UPLOADED_PENDING_SYNC'); return {'status':'DEFERRED_LOCAL_SYNC'}
        self.refresh.side_effect=check; self.assertEqual(self.call()['status'],'DEFERRED_LOCAL_SYNC'); self.assertEqual(self.state(),'UPLOADED_PENDING_SYNC')
    def test_registry_failure_preserves_pending(self):
        self.refresh.side_effect=TimeoutError; self.assertEqual(self.call()['status'],'REGISTRY_CHECK_FAILED'); self.assertEqual(self.state(),'UPLOADED_PENDING_SYNC')
    def test_retry_after_sync_exact_confirmation(self):
        self.pending(); self.refresh.return_value={'status':'UPDATED'}; self.load.side_effect=[([ROW],True,''),([ROW,MATCH],True,'')]
        self.assertEqual(self.call()['status'],'ALREADY_ARCHIVED'); self.assertEqual(self.state(),'ALREADY_ARCHIVED'); self.lookup.assert_not_called()
    def test_wrong_identity_not_archived(self):
        self.pending(); self.refresh.return_value={'status':'UPDATED'}
        for field,value in [('sha256','b'*64),('drive_file_id','wrong'),('drive_parent_folder_id','wrong'),('relative_path','folder/wrong.pdf'),('size_bytes',1)]:
            with self.subTest(field=field):
                self.load.side_effect=[([ROW],True,''),([ROW,{**MATCH,field:value}],True,'')]
                self.assertEqual(self.call()['status'],'REGISTRY_EVIDENCE_NOT_CONFIRMED'); self.assertEqual(self.state(),'UPLOADED_PENDING_SYNC')
    def test_repeat_archived_no_network(self):
        self.item['state']='ALREADY_ARCHIVED'; self.save(); self.assertEqual(self.call()['status'],'NOT_RECOVERABLE'); self.identity.assert_not_called()
    def test_refresh_metadata_only(self):
        self.refresh.return_value={'status':'UP_TO_DATE','total':34,'private':'not returned'}; res=r.refresh_archive_registry(); self.assertEqual(res['total'],34); self.assertEqual(res['cloud_writes'],0); self.assertNotIn('private',res); self.assertEqual(self.state(),'UPLOAD_UNCERTAIN')
    def test_identity_failure_no_remote(self):
        self.identity.side_effect=ValueError('synthetic'); self.assertRaises(ValueError,self.call); self.lookup.assert_not_called(); self.refresh.assert_not_called()

    def test_scan_hash_alone_cannot_close_uncertain_or_pending(self):
        for status in ('UPLOAD_UNCERTAIN','UPLOADED_PENDING_SYNC'):
            with self.subTest(status=status):
                item={**self.item,'state':status}
                state={'schema':1,'items':[item]}
                stage=Mock()
                i.merge_scan_into_queue(state=state,scan={'status':'OK','archive_registry_complete':True,'results':[]},archive_records=[ROW,MATCH],stage=stage,stage_root=self.root)
                self.assertEqual(item['state'],status); stage.assert_not_called()
