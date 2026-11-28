"""Synthetic offline regression tests; no Gmail/Drive connections."""
import hashlib
import unittest
from msp_bonds_vtb_reports.classifier import classify_document
from test_import_prepare_recovery import workbook, scan_fixture

class NonTargetReportsTests(unittest.TestCase):
    def test_execution_heading(self):
        r = scan_fixture(workbook('Отчёт об исполнении поручения № 12345'))
        self.assertEqual(r['skipped_non_target_attachments'], 1)
        self.assertEqual(sum(r['result_counts'].values()), 0)
    def test_short_notice_heading(self):
        r = scan_fixture(workbook('Уведомление о КД № 12345'))
        self.assertEqual(r['skipped_notification_attachments'], 1)
    def test_archive_hash_priority(self):
        data = workbook('Отчёт об исполнении поручения')
        r = scan_fixture(data, rows=[{'sha256': hashlib.sha256(data).hexdigest(), 'drive_verification': 'MATCH', 'drive_file_id': 'synthetic-file', 'drive_parent_folder_id': 'synthetic-folder'}])
        self.assertEqual(r['result_counts']['ALREADY_ARCHIVED'], 1)
        self.assertEqual(r['skipped_non_target_attachments'], 0)
    def test_target_titles_priority(self):
        for title in ('Брокерский отчёт', 'Выписка по счету депо', 'Отчёт о выплатах по ценным бумагам'):
            t = title+'\nОтчёт об исполнении поручения\nСчёт № TEST1234'
            r = classify_document(t, b'synthetic')
            self.assertEqual(r.status, 'CLASSIFIED')
            self.assertNotIn('ORDER_EXECUTION_REPORT', r.reasons)
    def test_metadata_is_not_evidence(self):
        r = classify_document('Неизвестный документ', b'synthetic', filename='Отчёт об исполнении поручения.pdf', subject='Уведомление о КД')
        self.assertEqual(r.status, 'NEEDS_REVIEW')
        self.assertIn('CATEGORY_EVIDENCE_INSUFFICIENT', r.reasons)
    def test_ambiguous_titles_remain_review(self):
        r = scan_fixture(workbook('Отчёт об исполнении поручения. Брокерский отчёт. Выписка по счету депо.'))
        self.assertEqual(r['result_counts']['NEEDS_REVIEW'], 1)
        self.assertEqual(r['skipped_non_target_attachments'], 0)
    def test_incidental_phrase_not_a_heading(self):
        r = classify_document('Приложение: отчёт об исполнении поручения', b'synthetic')
        self.assertIn('CATEGORY_EVIDENCE_INSUFFICIENT', r.reasons)

    # Synthetic reproductions of observed text structure; no personal data.
    CORPORATE = "Номер сообщения 123\nСведения О корпоративном действии эмитента\nОбщие сведения\nТип корпоративного действия\nПрочие сведения"
    EXECUTION = "\n".join(["Вводная строка"] * 12 + ["Отчет по форме 123 об исполнении поручения", "Банк ВТБ уведомляет об исполнении", "", "поручения клиента"])

    def test_observed_corporate_form(self):
        r = scan_fixture(workbook(self.CORPORATE))
        self.assertEqual(r['skipped_notification_attachments'], 1)
        self.assertEqual(sum(r['result_counts'].values()), 0)

    def test_observed_execution_form(self):
        r = scan_fixture(workbook(self.EXECUTION))
        self.assertEqual(r['skipped_non_target_attachments'], 1)
        self.assertEqual(sum(r['result_counts'].values()), 0)

    def test_extended_execution_needs_confirmation(self):
        r = classify_document(self.EXECUTION.replace('уведомляет об исполнении', 'сообщает о получении'), b'synthetic')
        self.assertIn('CATEGORY_EVIDENCE_INSUFFICIENT', r.reasons)

    def test_corporate_form_needs_all_three_signals(self):
        for fragment in ('Номер сообщения 123', 'Сведения О корпоративном действии эмитента', 'Тип корпоративного действия'):
            with self.subTest(fragment=fragment):
                r = classify_document(self.CORPORATE.replace(fragment, 'Нет сведений'), b'synthetic')
                self.assertIn('CATEGORY_EVIDENCE_INSUFFICIENT', r.reasons)

    def test_observed_forms_preserve_target_titles(self):
        for text in (self.CORPORATE, self.EXECUTION):
            for title in ('Брокерский отчёт', 'Выписка по счету депо', 'Отчёт о выплатах по ценным бумагам'):
                r = classify_document(text+'\n'+title+'\nСчёт № TEST1234', b'synthetic')
                self.assertEqual(r.status, 'CLASSIFIED')

    def test_observed_forms_preserve_positive_fallback(self):
        for text in (self.CORPORATE, self.EXECUTION):
            r = classify_document(text+'\nОтчет\nСделки\nДенежные операции\nСчёт № TEST1234', b'synthetic')
            self.assertEqual(r.category, 'Брокерский отчёт')
            self.assertEqual(r.status, 'CLASSIFIED')

    def test_observed_form_archive_priority(self):
        for text in (self.CORPORATE, self.EXECUTION):
            data = workbook(text)
            r = scan_fixture(data, rows=[{'sha256': hashlib.sha256(data).hexdigest(), 'drive_verification': 'MATCH', 'drive_file_id': 'synthetic-file', 'drive_parent_folder_id': 'synthetic-folder'}])
            self.assertEqual(r['result_counts']['ALREADY_ARCHIVED'], 1)
            self.assertEqual(r['skipped_non_target_attachments']+r['skipped_notification_attachments'], 0)

    REFERENCE_FORM = "\n".join(["Вводная строка"] * 12 + ["Код АБ 123 об исполнении поручения", "Реквизиты", "Номер поручения", "Реквизиты", "Банк уведомляет об исполнении", "", "поручения", "Счет депо TEST1234", "Ценные бумаги", "Сделки"])

    def test_reference_heading_with_depot_context(self):
        r = scan_fixture(workbook(self.REFERENCE_FORM))
        self.assertEqual(r['skipped_non_target_attachments'], 1)
        self.assertEqual(sum(r['result_counts'].values()), 0)

    def test_reference_heading_requires_independent_signals(self):
        for fragment in ('Код АБ 123 об исполнении поручения', 'уведомляет об исполнении', 'Счет депо', 'Ценные бумаги', 'Сделки'):
            with self.subTest(fragment=fragment):
                r = classify_document(self.REFERENCE_FORM.replace(fragment, 'Нет сведений'), b'synthetic')
                self.assertNotIn('ORDER_EXECUTION_REPORT', r.reasons)

    def test_reference_heading_requires_nearby_confirmation(self):
        t = self.REFERENCE_FORM.replace('Банк уведомляет об исполнении', 'Нет сведений') + '\n' + '\n'.join(['Строка'] * 20) + '\nБанк уведомляет об исполнении поручения'
        r = classify_document(t, b'synthetic')
        self.assertIn('CATEGORY_EVIDENCE_INSUFFICIENT', r.reasons)

    def test_reference_heading_preserves_target_titles(self):
        for title in ('Брокерский отчёт', 'Выписка по счету депо', 'Отчёт о выплатах по ценным бумагам'):
            r = classify_document(self.REFERENCE_FORM+'\n'+title, b'synthetic')
            self.assertIsNotNone(r.category)
            self.assertNotIn('ORDER_EXECUTION_REPORT', r.reasons)

    def test_reference_heading_preserves_depot_fallback(self):
        r = classify_document(self.REFERENCE_FORM+'\nДвижение ценных бумаг', b'synthetic')
        self.assertEqual(r.category, 'Выписка по счёту депо')

    def test_reference_heading_archive_hash_priority(self):
        data = workbook(self.REFERENCE_FORM)
        r = scan_fixture(data, rows=[{'sha256': hashlib.sha256(data).hexdigest(), 'drive_verification': 'MATCH', 'drive_file_id': 'synthetic-file', 'drive_parent_folder_id': 'synthetic-folder'}])
        self.assertEqual(r['result_counts']['ALREADY_ARCHIVED'], 1)
        self.assertEqual(r['skipped_non_target_attachments'], 0)
