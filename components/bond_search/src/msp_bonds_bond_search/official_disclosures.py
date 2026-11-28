"""Parse explicitly supplied official issuer-disclosure HTML.

This module performs no network requests and does not crawl for newer records.
It checks the exact ISIN and issue registration number in the supplied page,
extracts a narrowly defined paid-coupon observation, and keeps issue terms
missing unless the applicable legal document itself is supplied and reviewed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html.parser import HTMLParser
import re
import ssl
import unicodedata
from typing import Callable, Mapping
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .catalog import PolicySourceSnapshot, SnapshotError
from .evidence import DocumentProvenance, EvidenceItem, EvidenceKind, EvidenceState
from .manual_sources import ISSUE_TERMS_CLAIMS
from .routes import EVIDENCE_ROUTES

MAX_DISCLOSURE_BYTES = 5_000_000
_TRUSTED_INTERFAX_HOSTS = frozenset({"e-disclosure.ru", "www.e-disclosure.ru"})
_DATE = r"\d{2}\.\d{2}\.\d{4}"
_REGISTRATION = re.compile(r"4[A-Z0-9-]{8,30}", re.IGNORECASE)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


@dataclass(frozen=True)
class OfficialDisclosureCapture:
    status: str
    provenance: DocumentProvenance
    evidence: tuple[EvidenceItem, ...]
    identity_matched: bool
    currentness_established: bool = False


class OfficialDisclosureUnavailable(RuntimeError):
    """A single explicit public disclosure URL could not be read safely."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def fetch_interfax_disclosure_html(
    *,
    snapshot: PolicySourceSnapshot,
    source_name: str,
    document_url: str,
    expected_isin: str,
    expected_issue_registration_number: str,
    issuer: str,
    expected_issuer: str = "",
    retrieved_at: datetime | None = None,
    fetch: Callable[[str], bytes] | None = None,
) -> OfficialDisclosureCapture:
    """Fetch and parse one explicitly named Interfax HTML record, without search or redirects.

    The default transport uses the normal process proxy/environment settings,
    makes one unauthenticated HTTPS request, and never changes network policy.
    Tests and callers may inject an exact-byte fetcher.
    """
    _validate_registered_url(snapshot, source_name, document_url)
    try:
        payload = fetch(document_url) if fetch is not None else _fetch_html_once(document_url)
    except OfficialDisclosureUnavailable:
        raise
    except Exception:
        raise OfficialDisclosureUnavailable("official disclosure fetch failed") from None
    content = bytes(payload) if isinstance(payload, (bytes, bytearray)) else b""
    if not content:
        raise OfficialDisclosureUnavailable("official disclosure returned no HTML bytes")
    if len(content) > MAX_DISCLOSURE_BYTES:
        raise OfficialDisclosureUnavailable("official disclosure exceeds the size limit")
    text = _html_text(content)
    document_date = _publication_date(text)
    return capture_interfax_disclosure_html(
        snapshot=snapshot,
        source_name=source_name,
        document_url=document_url,
        artifact_bytes=content,
        document_date=document_date,
        retrieved_at=retrieved_at or datetime.now(timezone.utc),
        expected_isin=expected_isin,
        expected_issue_registration_number=expected_issue_registration_number,
        issuer=issuer,
        expected_issuer=expected_issuer,
    )


def capture_interfax_disclosure_html(
    *,
    snapshot: PolicySourceSnapshot,
    source_name: str,
    document_url: str,
    artifact_bytes: bytes,
    document_date: date,
    retrieved_at: datetime,
    expected_isin: str,
    expected_issue_registration_number: str,
    issuer: str,
    expected_issuer: str = "",
) -> OfficialDisclosureCapture:
    """Capture one official Interfax HTML record without declaring legal terms verified.

    The exact ISIN and issue registration number must both appear in the same
    record. A payout notice can confirm only that historical payment notice;
    it never establishes future coupon terms, a full schedule, amortization,
    offers, or that the issue decision is the latest edition.
    """
    if not isinstance(snapshot, PolicySourceSnapshot):
        raise ValueError("a current policy/source snapshot is required")
    try:
        snapshot.assert_fresh()
    except SnapshotError:
        raise ValueError("a current policy/source snapshot is required") from None
    source = _validate_registered_url(snapshot, source_name, document_url)

    if not isinstance(artifact_bytes, (bytes, bytearray)) or not artifact_bytes:
        raise ValueError("official disclosure bytes are required")
    content = bytes(artifact_bytes)
    if len(content) > MAX_DISCLOSURE_BYTES:
        raise ValueError("official disclosure exceeds the size limit")
    if retrieved_at.tzinfo is None:
        raise ValueError("retrieved_at must be timezone-aware")
    if not isinstance(document_date, date):
        raise ValueError("document_date must be a date")
    if document_date > retrieved_at.date():
        raise ValueError("document date cannot be in the future")
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", expected_isin.upper()):
        raise ValueError("expected ISIN has an invalid shape")
    if not _REGISTRATION.fullmatch(expected_issue_registration_number):
        raise ValueError("expected issue registration number is invalid")
    if not issuer.strip():
        raise ValueError("issuer is required")

    text = _html_text(content)
    if _publication_date(text) != document_date:
        raise ValueError("document date differs from the official disclosure header")
    message_match = re.search(r"код\s*сообщ\w*\s*[:№]?\s*(\d{4,})", text, re.IGNORECASE)
    document_id = message_match.group(1) if message_match else ""
    if not document_id:
        raise ValueError("official disclosure message ID is missing")

    provenance = DocumentProvenance(
        document_type="COUPON_NOTICE",
        document_id=document_id,
        issuer=issuer,
        isin=expected_isin.upper(),
        issue_registration_number=expected_issue_registration_number,
        document_date=document_date,
        source_name=source.name,
        source_url=document_url,
        retrieved_at=retrieved_at,
        artifact_sha256=sha256(content).hexdigest(),
        locator=f"message {document_id}",
        source_status="UNKNOWN",
    )

    normalized = _normal_text(text)
    identity_matched = (
        _contains_identifier(text, expected_isin.upper())
        and _contains_identifier(text, expected_issue_registration_number)
        and (not expected_issuer or _issuer_matches(text, expected_issuer))
    )
    if not identity_matched:
        return OfficialDisclosureCapture(
            "IDENTITY_MISMATCH", provenance, (), False, False,
        )

    evidence: list[EvidenceItem] = []
    payout = _parse_paid_coupon_observation(text, normalized)
    if payout is not None:
        evidence.append(EvidenceItem(
            claim="historical_coupon_payment",
            value=payout,
            unit="RUB per bond",
            source_name=source.name,
            source_url=document_url,
            kind=EvidenceKind.PRIMARY,
            state=EvidenceState.CONFIRMED,
            retrieved_at=retrieved_at,
            document_date=document_date,
            method="strict_interfax_disclosure_html_parse",
            note="Confirms only the explicit historical payment notice; it is not a future legal term.",
            document_provenance=provenance,
        ))

    # A payment report does not establish the current issue decision or its
    # remaining legal terms. Keep these claims explicitly open.
    for claim in ISSUE_TERMS_CLAIMS:
        evidence.append(EvidenceItem(
            claim=claim,
            value=None,
            unit="",
            source_name=source.name,
            source_url=document_url,
            kind=EvidenceKind.PRIMARY,
            state=EvidenceState.MISSING,
            retrieved_at=retrieved_at,
            document_date=document_date,
            method="interfax_disclosure_html_scope_limit",
            note="This disclosure does not by itself establish the current legal issue term.",
            document_provenance=provenance,
        ))
    return OfficialDisclosureCapture(
        "IDENTITY_MATCHED_REVIEW_REQUIRED", provenance, tuple(evidence), True, False,
    )


def _html_text(content: bytes) -> str:
    try:
        html = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        html = content.decode("cp1251", errors="replace")
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return " ".join(" ".join(parser.parts).split())


def _publication_date(text: str) -> date:
    match = re.search(r"(?<!\d)(\d{2}\.\d{2}\.\d{4})\s+\d{2}:\d{2}(?!\d)", text)
    if match is None:
        raise OfficialDisclosureUnavailable("official disclosure publication date was not found")
    try:
        return datetime.strptime(match.group(1), "%d.%m.%Y").date()
    except ValueError:
        raise OfficialDisclosureUnavailable("official disclosure publication date is invalid") from None


def _normal_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _contains_identifier(text: str, identifier: str) -> bool:
    return re.search(rf"(?<![A-Z0-9]){re.escape(identifier)}(?![A-Z0-9])", text, re.IGNORECASE) is not None


def _issuer_matches(text: str, expected_issuer: str) -> bool:
    expected = [token for token in re.findall(r"[^\W_]+", _normal_text(expected_issuer))]
    observed = set(re.findall(r"[^\W_]+", _normal_text(text)))
    return bool(expected) and all(token in observed for token in expected)


def _parse_paid_coupon_observation(text: str, normalized: str) -> Mapping[str, object] | None:
    if "выплаченные доходы" not in normalized or "объем" not in normalized:
        return None
    period = re.search(
        rf"(?P<number>\d+)\s*[-–—]?\s*(?:й|го)?\s*купонный период\s*:?\s*"
        rf"дата начала(?: купонного периода)?\s*(?P<start>{_DATE})"
        rf".*?окончания(?: купонного периода)?\s*(?P<end>{_DATE})",
        text,
        re.IGNORECASE,
    )
    amount = re.search(
        r"размер выплаченных доходов.{0,500}?на одну ценную бумагу эмитента\s*:\s*"
        r"(?P<amount>\d[\d\s\u00a0]*(?:[,.]\d{1,4})?)",
        text,
        re.IGNORECASE,
    )
    due = re.search(
        r"2\.9\..{0,1000}?дата, в которую обязанность по выплате доходов"
        r".{0,1200}?исполнена.{0,250}?:\s*(?P<date>" + _DATE + r")",
        text,
        re.IGNORECASE,
    )
    execution = re.search(
        r"2\.10\..{0,1800}?объем.{0,500}?исполнено.{0,300}?:\s*"
        r"(?P<percent>\d{1,3}(?:[,.]\d+)?)\s*%",
        text,
        re.IGNORECASE,
    )
    if not (period and amount and due and execution):
        return None
    try:
        start = datetime.strptime(period.group("start"), "%d.%m.%Y").date()
        end = datetime.strptime(period.group("end"), "%d.%m.%Y").date()
        payment_date = datetime.strptime(due.group("date"), "%d.%m.%Y").date()
        coupon_amount = Decimal(amount.group("amount").replace("\u00a0", "").replace(" ", "").replace(",", "."))
        execution_percent = Decimal(execution.group("percent").replace(",", "."))
    except (InvalidOperation, ValueError, OverflowError):
        return None
    if start >= end or not Decimal(0) < coupon_amount <= Decimal(1_000_000) or not Decimal(0) <= execution_percent <= Decimal(100):
        return None
    return {
        "coupon_number": int(period.group("number")),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "payment_date": payment_date.isoformat(),
        "amount_per_bond_rub": format(coupon_amount.normalize(), "f"),
        "execution_percent": format(execution_percent.normalize(), "f"),
    }


def _validate_registered_url(
    snapshot: PolicySourceSnapshot, source_name: str, document_url: str,
):
    if not isinstance(snapshot, PolicySourceSnapshot):
        raise ValueError("a current policy/source snapshot is required")
    try:
        snapshot.assert_fresh()
    except SnapshotError:
        raise ValueError("a current policy/source snapshot is required") from None
    if source_name != "Интерфакс — раскрытие" or source_name not in EVIDENCE_ROUTES["coupon_terms"]:
        raise ValueError("source is not registered for issue-term disclosures")
    try:
        source = snapshot.resource(source_name)
    except SnapshotError:
        raise ValueError("source is absent from the current resource register") from None
    if not source.url.strip():
        raise ValueError("registered disclosure source URL is empty")
    _validate_url(document_url, source.url)
    try:
        registered_host = urlsplit(source.url).hostname
        document_host = urlsplit(document_url).hostname
    except (TypeError, ValueError):
        raise ValueError("invalid official disclosure URL") from None
    if source_name == "Интерфакс — раскрытие" and (
        registered_host not in _TRUSTED_INTERFAX_HOSTS or document_host not in _TRUSTED_INTERFAX_HOSTS
    ):
        raise ValueError("Interfax source must use the registered official e-disclosure.ru host")
    return source


def _fetch_html_once(document_url: str) -> bytes:
    try:
        parts = urlsplit(document_url)
    except (TypeError, ValueError):
        raise OfficialDisclosureUnavailable("invalid official disclosure URL") from None
    if parts.hostname not in _TRUSTED_INTERFAX_HOSTS:
        raise OfficialDisclosureUnavailable("Interfax host is not allowed for this request")
    request = Request(
        document_url,
        headers={"User-Agent": "MSPBonds-Research/0.1", "Accept": "text/html,application/xhtml+xml"},
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=8.0) as response:
            if response.getcode() != 200:
                raise OfficialDisclosureUnavailable("official disclosure returned a non-success status")
            if urlsplit(response.geturl()).hostname not in _TRUSTED_INTERFAX_HOSTS:
                raise OfficialDisclosureUnavailable("official disclosure redirected outside the registered host")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise OfficialDisclosureUnavailable("official disclosure is not an HTML page")
            payload = response.read(MAX_DISCLOSURE_BYTES + 1)
    except HTTPError as exc:
        code = int(exc.code)
        if 300 <= code < 400:
            raise OfficialDisclosureUnavailable("official disclosure redirect was blocked") from None
        raise OfficialDisclosureUnavailable(f"official disclosure HTTP error {code}") from None
    except OfficialDisclosureUnavailable:
        raise
    except (URLError, TimeoutError, OSError, ssl.SSLError):
        raise OfficialDisclosureUnavailable("official disclosure transport failed") from None
    if len(payload) > MAX_DISCLOSURE_BYTES:
        raise OfficialDisclosureUnavailable("official disclosure exceeds the size limit")
    return payload


def _validate_url(document_url: str, registered_url: str) -> None:
    try:
        document = urlsplit(document_url)
        registered = urlsplit(registered_url)
        document_port = document.port
        registered_port = registered.port
    except (TypeError, ValueError):
        raise ValueError("invalid official disclosure URL") from None
    if (
        document.scheme != "https" or not document.hostname or document.username or document.password
        or document_port or document.fragment or registered.scheme != "https" or not registered.hostname
        or registered.username or registered.password or registered_port
    ):
        raise ValueError("official disclosure URL must be a safe HTTPS URL")
    document_host = document.hostname.lower().removeprefix("www.")
    registered_host = registered.hostname.lower().removeprefix("www.")
    if document_host != registered_host and not document_host.endswith("." + registered_host):
        raise ValueError("official disclosure URL differs from the registered source host")
