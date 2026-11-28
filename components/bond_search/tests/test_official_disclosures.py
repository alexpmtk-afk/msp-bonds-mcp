from datetime import date, datetime, timezone
import hashlib
import unittest

from msp_bonds_bond_search.catalog import PolicySourceSnapshot, RESOURCE_HEADERS, RULE_HEADERS
from msp_bonds_bond_search.evidence import EvidenceState
from msp_bonds_bond_search.official_disclosures import (
    capture_interfax_disclosure_html,
    fetch_interfax_disclosure_html,
)
from msp_bonds_bond_search.manual_sources import ISSUE_TERMS_CLAIMS


NOW = datetime.now(timezone.utc)
SOURCE_NAME = "Интерфакс — раскрытие"
ISIN = "RU000A10FGE6"
REGISTRATION = "4B02-05-16970-A-001P"
URL = "https://www.e-disclosure.ru/portal/event.aspx?EventId=test-event"

DISCLOSURE_HTML = """<!doctype html><html><body>
<p>Сообщение компании</p><p>16.11.2026 15:00</p><p>код сообщения: 3825307</p>
<h2>ПАО «ИЭК ХОЛДИНГ»</h2><h3>Выплаченные доходы или иные выплаты</h3>
<p>Биржевые облигации серии 001P-05, регистрационный номер выпуска
4B02-05-16970-A-001P от 08.06.2026, ISIN – RU000A10FGE6.</p>
<p>2.3. Отчетный (купонный) период: 5-й купонный период:
дата начала купонного периода 17.10.2026 и окончания купонного периода 16.11.2026.</p>
<p>2.5. Размер выплаченных доходов, а также иных выплат в расчете на одну
ценную бумагу эмитента: 12,95 (Двенадцать целых девяносто пять сотых) рублей.</p>
<p>2.9. Дата, в которую обязанность по выплате доходов по ценным бумагам
должна быть исполнена: 16.11.2026.</p>
<p>2.10. Объем (в процентах), в котором исполнено обязательство по выплате
доходов по ценным бумагам эмитента: 100 %.</p>
</body></html>""".encode("utf-8")


def snapshot():
    rules = [list(RULE_HEADERS), ["1", "Test", "Approved rule", "", "", "УТВЕРЖДЕНО", ""]]
    resources = [
        list(RESOURCE_HEADERS),
        ["Primary", SOURCE_NAME, "https://www.e-disclosure.ru/poisk-po-soobshheniyam",
         "Issuer disclosure", "Use exact record and amendments", "TEST", "coupon_terms"],
    ]
    return PolicySourceSnapshot.from_sheet_rows(rules, resources, fetched_at=NOW)


def capture(**overrides):
    args = {
        "snapshot": snapshot(), "source_name": SOURCE_NAME, "document_url": URL,
        "artifact_bytes": DISCLOSURE_HTML, "document_date": date(2026, 11, 16),
        "retrieved_at": NOW, "expected_isin": ISIN,
        "expected_issue_registration_number": REGISTRATION, "issuer": "ПАО ИЭК ХОЛДИНГ",
        "expected_issuer": "ИЭК ХОЛДИНГ",
    }
    args.update(overrides)
    return capture_interfax_disclosure_html(**args)


class OfficialDisclosureCaptureTests(unittest.TestCase):
    def test_exact_interfax_paid_coupon_notice_keeps_issue_terms_open(self):
        result = capture()
        self.assertEqual(result.status, "IDENTITY_MATCHED_REVIEW_REQUIRED")
        self.assertTrue(result.identity_matched)
        self.assertFalse(result.currentness_established)
        self.assertEqual(result.provenance.document_id, "3825307")
        self.assertEqual(result.provenance.artifact_sha256, hashlib.sha256(DISCLOSURE_HTML).hexdigest())
        by_claim = {item.claim: item for item in result.evidence}
        payment = by_claim["historical_coupon_payment"]
        self.assertIs(payment.state, EvidenceState.CONFIRMED)
        self.assertEqual(payment.value, {
            "coupon_number": 5,
            "period_start": "2026-10-17",
            "period_end": "2026-11-16",
            "payment_date": "2026-11-16",
            "amount_per_bond_rub": "12.95",
            "execution_percent": "100",
        })
        self.assertEqual(payment.document_provenance, result.provenance)
        self.assertTrue(all(by_claim[claim].state is EvidenceState.MISSING for claim in ISSUE_TERMS_CLAIMS))

    def test_other_isin_or_issue_number_cannot_be_attached_to_target(self):
        altered = DISCLOSURE_HTML.replace(ISIN.encode(), b"RU000A10FZ24")
        result = capture(artifact_bytes=altered)
        self.assertEqual(result.status, "IDENTITY_MISMATCH")
        self.assertEqual(result.evidence, ())

    def test_issue_registration_number_must_match_exactly(self):
        result = capture(expected_issue_registration_number="4B02-06-16970-A-001P")
        self.assertEqual(result.status, "IDENTITY_MISMATCH")
        self.assertEqual(result.evidence, ())

    def test_supplied_document_date_must_match_disclosure_header(self):
        with self.assertRaisesRegex(ValueError, "differs from the official disclosure header"):
            capture(document_date=date(2026, 11, 17))

    def test_disclosure_host_must_match_registered_interfax_host(self):
        with self.assertRaisesRegex(ValueError, "registered source host"):
            capture(document_url="https://e-disclosure.azipi.ru/messages/4553906/")

    def test_explicit_fetch_reads_one_registered_html_url_and_uses_page_date(self):
        calls = []
        result = fetch_interfax_disclosure_html(
            snapshot=snapshot(), source_name=SOURCE_NAME, document_url=URL,
            expected_isin=ISIN, expected_issue_registration_number=REGISTRATION,
            issuer="ПАО ИЭК ХОЛДИНГ", expected_issuer="ИЭК ХОЛДИНГ",
            retrieved_at=NOW, fetch=lambda url: calls.append(url) or DISCLOSURE_HTML,
        )
        self.assertEqual(calls, [URL])
        self.assertEqual(result.provenance.document_date, date(2026, 11, 16))
        self.assertEqual(result.provenance.document_id, "3825307")
        self.assertEqual(result.status, "IDENTITY_MATCHED_REVIEW_REQUIRED")

    def test_fetch_rejects_unregistered_host_before_calling_transport(self):
        calls = []
        with self.assertRaisesRegex(ValueError, "registered source host"):
            fetch_interfax_disclosure_html(
                snapshot=snapshot(), source_name=SOURCE_NAME,
                document_url="https://e-disclosure.azipi.ru/messages/4553906/",
                expected_isin=ISIN, expected_issue_registration_number=REGISTRATION,
                issuer="ПАО ИЭК ХОЛДИНГ", retrieved_at=NOW,
                fetch=lambda url: calls.append(url) or DISCLOSURE_HTML,
            )
        self.assertEqual(calls, [])

    def test_incomplete_payment_notice_does_not_create_a_payment_fact(self):
        # Remove the execution percentage while preserving the exact identity.
        stripped = DISCLOSURE_HTML.replace(b"100 %.", b"not stated.")
        result = capture(artifact_bytes=stripped)
        self.assertNotIn("historical_coupon_payment", {item.claim for item in result.evidence})
        by_claim = {item.claim: item for item in result.evidence}
        self.assertTrue(all(by_claim[claim].state is EvidenceState.MISSING for claim in ISSUE_TERMS_CLAIMS))


if __name__ == "__main__":
    unittest.main()
