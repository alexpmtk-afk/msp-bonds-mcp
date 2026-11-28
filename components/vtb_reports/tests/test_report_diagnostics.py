import json
import unittest
from unittest.mock import Mock
from msp_bonds_vtb_reports.report_diagnostics import diagnose_bytes, diagnose_gmail_attachment
from msp_bonds_vtb_reports.classifier import classify_document
from test_import_prepare_recovery import workbook

class ReportDiagnosticsTests(unittest.TestCase):
    def test_unknown_inventory_variant_and_no_raw_data(self):
        t="SECRETNAME 9382746123\nОтчет по форме об исполнении поручения\nУведомляет об исполнении инвентарного поручения\nСчёт № PRIVATE1234"
        r=diagnose_bytes(workbook(t))
        self.assertEqual(r['category_reason'],'CATEGORY_EVIDENCE_INSUFFICIENT')
        self.assertTrue(r['rule_trace']['order_title_extended'])
        self.assertFalse(r['rule_trace']['order_confirmation'])
        self.assertIn('INVENTORY',json.dumps(r['structural_lines']))
        for secret in ('SECRETNAME','9382746123','PRIVATE1234'):
            self.assertNotIn(secret,json.dumps(r))
    def test_target_priority_and_trace(self):
        r=diagnose_bytes(workbook('Брокерский отчёт\nОтчет об исполнении поручения\nСчёт № TEST1234'))
        self.assertEqual(r['category'],'Брокерский отчёт')
        self.assertIsNone(r['rule_trace']['order_heading_exact'])
    def test_ambiguous(self):
        r=diagnose_bytes(workbook('Брокерский отчёт. Выписка по счету депо.'))
        self.assertEqual(r['category_reason'],'AMBIGUOUS_CATEGORY_TITLES')
    def test_explicit_notice(self):
        r=diagnose_bytes(workbook('Уведомление о КД'))
        self.assertTrue(r['rule_trace']['notice_heading_short'])
    def test_bad_document(self):
        r=diagnose_bytes(b'not a report')
        self.assertEqual(r['reason_codes'],['DOCUMENT_TEXT_UNAVAILABLE'])
    def setUp(self):
        self.data=workbook('Отчет об исполнении поручения')
        self.message={'id':'synthetic-message','headers':{'from':'test@example.invalid'},'attachments':[{'part_id':'1','attachment_id':'synthetic','filename':'x.xlsx','size':len(self.data)}]}
        self.get=Mock(return_value=self.message)
        self.download=Mock(return_value={'data':self.data,'attachment_id':'synthetic','part_id':'1'})
    def call(self, **kwargs):
        opts=dict(message_id='synthetic-message',part_id='1',allowed_senders='test@example.invalid',get_message=self.get,get_attachment=self.download);opts.update(kwargs)
        return diagnose_gmail_attachment(**opts)
    def test_exact_attachment(self):
        r=self.call();self.assertEqual(r['category_reason'],'ORDER_EXECUTION_REPORT');self.assertEqual(r['cloud_writes'],0)
    def test_sender_denied(self):
        self.message['headers']['from']='other@example.invalid';self.assertEqual(self.call()['reason'],'SENDER_NOT_ALLOWLISTED');self.download.assert_not_called()
    def test_missing_allowlist(self):
        self.assertEqual(self.call(allowed_senders='')['reason'],'SENDER_ALLOWLIST_NOT_CONFIGURED');self.get.assert_not_called()
    def test_bad_selector(self):
        self.assertEqual(self.call(part_id='../secret')['reason'],'INVALID_ATTACHMENT_SELECTOR');self.get.assert_not_called()
    def test_oversized(self):
        self.message['attachments'][0]['size']=100000000;self.assertEqual(self.call()['reason'],'ATTACHMENT_SIZE_OUT_OF_RANGE');self.download.assert_not_called()
    def test_ambiguous_part(self):
        self.message['attachments']*=2;self.assertEqual(self.call()['reason'],'ATTACHMENT_MISSING_OR_AMBIGUOUS');self.download.assert_not_called()
    def test_attachment_mismatch(self):
        self.download.return_value['attachment_id']='other';self.assertEqual(self.call()['reason'],'ATTACHMENT_ID_OR_SIZE_MISMATCH')

    def test_manifest_reused_for_exact_attachment(self):
        self.call()
        self.assertIs(self.download.call_args.kwargs['message_manifest'], self.message)

    def test_real_gateway_uses_original_opaque_id(self):
        import base64
        from unittest.mock import patch, MagicMock
        from msp_bonds_google import workspace
        service = MagicMock()
        messages = service.users.return_value.messages.return_value
        messages.get.return_value.execute.return_value = {
            'id':'synthetic-message','payload':{'headers':[{'name':'From','value':'test@example.invalid'}],
                'parts':[{'partId':'1','filename':'x.xlsx','mimeType':'application/octet-stream',
                          'body':{'size':len(self.data),'attachmentId':'first-id'}}]}}
        messages.attachments.return_value.get.return_value.execute.return_value = {'data':base64.urlsafe_b64encode(self.data).decode()}
        with patch.object(workspace,'_gmail',return_value=service):
            r=self.call(get_message=workspace.gmail_message,get_attachment=workspace.gmail_attachment_bytes)
        self.assertEqual(r['category_reason'],'ORDER_EXECUTION_REPORT')
        self.assertEqual(messages.get.call_count,1)
        messages.attachments.return_value.get.assert_called_once_with(userId='me',messageId='synthetic-message',id='first-id')

    def test_gateway_manifest_wrong_message_rejected(self):
        from unittest.mock import patch, MagicMock
        from msp_bonds_google import workspace
        service=MagicMock()
        with patch.object(workspace,'_gmail',return_value=service):
            with self.assertRaises(workspace.InputError):
                workspace.gmail_attachment_bytes('different-message','1',message_manifest=self.message)
        service.users.assert_not_called()

    def test_gateway_manifest_size_mismatch_rejected(self):
        import base64
        from unittest.mock import patch, MagicMock
        from msp_bonds_google import workspace
        service=MagicMock()
        messages=service.users.return_value.messages.return_value
        messages.attachments.return_value.get.return_value.execute.return_value={'data':base64.urlsafe_b64encode(b'wrong').decode()}
        with patch.object(workspace,'_gmail',return_value=service):
            with self.assertRaises(workspace.InputError):
                workspace.gmail_attachment_bytes('synthetic-message','1',message_manifest=self.message)
        messages.get.assert_not_called()
