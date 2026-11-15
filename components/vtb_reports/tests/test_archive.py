"""All tests use synthetic files in a temporary directory; never real broker reports."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from msp_bonds_vtb_reports import (
    ENV_REPORT_ROOT,
    REPORT_CATEGORIES,
    archive_status,
    check_report_files,
    inventory,
    list_reports,
)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for folder in REPORT_CATEGORIES:
            (self.root / folder).mkdir()
        self.account_dir = self.root / REPORT_CATEGORIES[0] / 'Синтетический-счёт'
        self.account_dir.mkdir()
        (self.account_dir / 'sample.pdf').write_bytes(b'%PDF-1.7\nSynthetic test data only')
        other = self.root / REPORT_CATEGORIES[1] / 'Новый-субсчёт'
        other.mkdir()
        with ZipFile(other / 'sample.xlsx', 'w') as z:
            z.writestr('[Content_Types].xml', '<Types/>')
            z.writestr('xl/workbook.xml', '<workbook/>')
        self.context = patch.dict(os.environ, {ENV_REPORT_ROOT: str(self.root)})
        self.context.start()
        self.addCleanup(self.context.stop)

    def test_status_discovers_unbounded_account_names(self):
        data = archive_status()
        self.assertEqual(data['status'], 'OK')
        self.assertEqual(data['total_files'], 2)
        self.assertFalse(data['writes_enabled'])
        self.assertIn('Новый-субсчёт', data['sections'][1]['accounts'])

    def test_pagination_and_filters(self):
        data = list_reports(report_type=REPORT_CATEGORIES[0], limit=1)
        self.assertEqual(data['total_matches'], 1)
        self.assertEqual(data['reports'][0]['name'], 'sample.pdf')
        self.assertEqual(list_reports(offset=1, limit=1)['returned'], 1)
        self.assertEqual(list_reports(report_type='INVALID')['status'], 'INVALID_FILTER')

    def test_format_validation(self):
        data = check_report_files(max_files=10)
        self.assertEqual(data['checked'], 2)
        self.assertEqual(data['results']['PASS'], 2)
        (self.account_dir / 'invalid.pdf').write_bytes(b'NOT-PDF')
        data = check_report_files(max_files=10)
        self.assertEqual(data['results']['FAIL'], 1)

    def test_missing_configuration_is_not_silently_defaulted(self):
        with patch.dict(os.environ, {ENV_REPORT_ROOT: ''}):
            self.assertEqual(inventory()['status'], 'NOT_CONFIGURED')

    def test_missing_root(self):
        with patch.dict(os.environ, {ENV_REPORT_ROOT: str(self.root / 'absent')}):
            self.assertEqual(inventory()['status'], 'UNAVAILABLE')

    def test_no_mutation_of_report_bytes(self):
        p = self.account_dir / 'sample.pdf'
        before = p.read_bytes()
        archive_status()
        list_reports()
        check_report_files()
        self.assertEqual(before, p.read_bytes())


if __name__ == '__main__':
    unittest.main()
