"""Content-based VTB report classification and metadata extraction."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from .archive import REPORT_CATEGORIES

BROKER_REPORT = REPORT_CATEGORIES[0]
DEPOT_STATEMENT = REPORT_CATEGORIES[1]
SECURITIES_PAYMENTS = REPORT_CATEGORIES[2]

_DATE = r"(?:\d{1,2}[./]\d{1,2}[./]\d{4}|\d{4}-\d{2}-\d{2})"
_PERIOD_RE = re.compile(
    rf"(?:отч[её]тн\w*\s+период|период(?:\s+операций)?|за\s+период|операци\w*\s+с)"
    rf"\s*[:№]?\s*(?:с\s*)?({_DATE})\s*(?:по|до|—|–|-)\s*({_DATE})",
    re.IGNORECASE,
)
_REPORT_DATE_RE = re.compile(
    rf"(?:отч[её]тн\w*\s+дата|дата\s+отч[её]т\w*|дата\s+выписк\w*|"
    rf"по\s+состоянию\s+на|состояни\w*\s+на)\D{{0,24}}({_DATE})",
    re.IGNORECASE,
)

_GENERATED_DATE_RE = re.compile(
    rf"(?:дата\s+формирован\w*|сформирован\w*)\D{{0,24}}({_DATE})",
    re.IGNORECASE,
)
_ACCOUNT_RE = re.compile(
    r"(?<!\w)(?:номер\s+)?(?:(?:брокерского|брокерский|лицевого|лицевой|клиентского|клиентский)\s+)?"
    r"(?:суб)?сч[её]т(?:а|у|е|ом)?(?:\s+депо)?"
    r"\s*(?:№|no\.?|номер)?\s*[:#]?\s*"
    r"(?P<account>(?=[A-ZА-ЯЁ0-9./_-]{4,})(?=[A-ZА-ЯЁ0-9./_-]*\d)"
    r"[A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9./_-]{3,})",
    re.IGNORECASE,
)
_PRIMARY_ACCOUNT_LABEL_RE = re.compile(
    r"(?<!\w)(?:№\s*субсч[её]та|номер\s+сч[её]та\s+депо)(?!\w)",
    re.IGNORECASE,
)
_PRIMARY_ACCOUNT_RE = re.compile(
    r"(?<!\w)(?:№\s*субсч[её]та|номер\s+сч[её]та\s+депо)\s*[:#]?\s*"
    r"(?P<account>(?=[A-ZА-ЯЁ0-9./_-]{4,})(?=[A-ZА-ЯЁ0-9./_-]*\d)"
    r"[A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9./_-]{3,})",
    re.IGNORECASE,
)


_TITLE_PATTERNS = {
    BROKER_REPORT: re.compile(
        r"\bброкерск\w*\s+отч[её]т\w*\b|\bотч[её]т\w*\s+брокер\w*\b"
    ),
    DEPOT_STATEMENT: re.compile(
        r"\bвыписк\w*\s+по\s+счет\w*\s+депо\b|"
        r"\bвыписк\w*\s+о\s+состоян\w*\s+счет\w*\s+депо\b|"
        r"\bдепозитарн\w*\s+выписк\w*\b"
    ),
    SECURITIES_PAYMENTS: re.compile(
        r"\bотч[её]т\w*\s+о\s+выплат\w*\s+по\s+ценн\w*\s+бумаг\w*\b"
    ),
}

_CORPORATE_NOTIFICATION_RE = re.compile(
    r"\bуведомлен\w*\b.{0,100}\b(?:корпоративн\w*\s+действ\w*|купон\w*|дивиденд\w*|погашен\w*|оферт\w*)\b"
    r"|\b(?:корпоративн\w*\s+действ\w*|купон\w*|дивиденд\w*|погашен\w*|оферт\w*)\b.{0,100}\bуведомлен\w*\b"
)


# Only explicit headings in the opening part of extracted content; metadata is not evidence.
_ORDER_EXECUTION_HEADING_RE = re.compile(
    r"(?:^|\n)[ \t]*отчет[ \t]+об[ \t]+исполнении[ \t]+поручения(?=\s|[№:.,-]|$)"
)
_SHORT_CORPORATE_HEADING_RE = re.compile(
    r"(?:^|\n)[ \t]*уведомление[ \t]+о[ \t]+кд(?=\s|[№:.,-]|$)"
)


@dataclass(frozen=True)
class ClassificationResult:
    category: Optional[str]
    account_ids: tuple[str, ...]
    period_start: Optional[str]
    period_end: Optional[str]
    report_date: Optional[str]
    generated_date: Optional[str]
    sha256: str
    confidence: float
    status: str
    reasons: tuple[str, ...]


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


def _parse_date(value: str) -> Optional[str]:
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _unique_dates(pattern: re.Pattern[str], text: str) -> tuple[Optional[str], bool]:
    parsed = {_parse_date(match.group(1)) for match in pattern.finditer(text)}
    parsed.discard(None)
    if len(parsed) == 1:
        return next(iter(parsed)), False
    if len(parsed) > 1:
        return None, True
    return None, False


def _category_from_content(text: str, trace: Optional[dict] = None) -> tuple[Optional[str], float, Optional[str]]:
    normalized = _normalized(text)
    exact = [category for category, pattern in _TITLE_PATTERNS.items() if pattern.search(normalized)]
    if trace is not None:
        trace["target_titles"] = exact
    if len(exact) == 1:
        return exact[0], 0.99, None
    if len(exact) > 1:
        return None, 0.0, "AMBIGUOUS_CATEGORY_TITLES"
    order_heading_exact = bool(_ORDER_EXECUTION_HEADING_RE.search(normalized[:1000]))
    if trace is not None:
        trace["order_heading_exact"] = order_heading_exact
    if order_heading_exact:
        return None, 0.0, "ORDER_EXECUTION_REPORT"
    notice_heading_short = bool(_SHORT_CORPORATE_HEADING_RE.search(normalized[:1000]))
    if trace is not None:
        trace["notice_heading_short"] = notice_heading_short
    if notice_heading_short:
        return None, 0.0, "CORPORATE_ACTION_NOTIFICATION"
    notice_legacy = bool(_CORPORATE_NOTIFICATION_RE.search(normalized))
    if trace is not None:
        trace["notice_legacy"] = notice_legacy
    if notice_legacy:
        return None, 0.0, "CORPORATE_ACTION_NOTIFICATION"

    has_report = bool(re.search(r"\bотч[её]т\w*\b", normalized))
    has_deals = bool(re.search(r"\bсделк\w*\b", normalized))
    has_cash_or_movement = bool(re.search(r"\b(?:денежн\w*|остатк\w*|движени\w*)\b", normalized))
    strong_broker_evidence = has_report and has_deals and has_cash_or_movement
    has_depot = bool(re.search(r"\bсчет\w*\s+депо\b", normalized))
    has_securities = bool(re.search(r"\bценн\w*\s+бумаг\w*\b", normalized))
    has_depot_movement = bool(re.search(r"\b(?:движени\w*|остатк\w*)\b", normalized))
    has_payment = bool(
        re.search(r"\bкупон\w*\b", normalized)
        and re.search(r"\b(?:выплат\w*|налог\w*)\b", normalized)
        and re.search(r"\b(?:отч[её]т\w*|ценн\w*\s+бумаг\w*)\b", normalized)
    )

    if trace is not None:
        trace.update(report_word=has_report, deal_word=has_deals,
                     cash_or_movement=has_cash_or_movement, broker_fallback=strong_broker_evidence,
                     depot_account_phrase=has_depot, securities_phrase=has_securities,
                     depot_movement=has_depot_movement, depot_fallback=has_depot and has_securities and has_depot_movement,
                     payment_fallback=has_payment)
    # Brokerage reports can include securities/depot balances and coupon details.
    # Deal and cash-operation sections are stronger content evidence than those overlaps.
    if strong_broker_evidence:
        return BROKER_REPORT, 0.90, None
    if has_depot and has_securities and has_depot_movement:
        return DEPOT_STATEMENT, 0.82, None
    if has_payment:
        return SECURITIES_PAYMENTS, 0.82, None
    # Additional forms observed in real VTB PDFs. Require two independent
    # content signals (three for corporate messages), not filename or subject.
    # Existing positive and ambiguous target-title checks and positive fallback
    # classifications above retain priority.
    lines = [" ".join(re.findall(r"\w+", line)) for line in normalized.splitlines()[:25]]
    notice_phrase = any("о корпоративном действии" in line for line in lines[:5])
    notice_type = any(line.startswith("тип корпоративного действия") for line in lines[:15])
    notice_message = any("сообщения" in line.split() for line in lines[:5])
    order_title = any(re.fullmatch(r"отчет(?: \w+){0,4} об исполнении поручения", line) for line in lines)
    order_confirmation = "уведомляет об исполнении поручения" in " ".join(re.findall(r"\w+", normalized))
    notice = notice_phrase and notice_type and notice_message
    # A depot execution confirmation may have a reference code instead of a report noun.
    # Require an opening heading, a nearby explicit confirmation and depot context.
    # This only runs after all positive target-title and fallback checks above.
    order_reference_title = False
    order_near_confirmation = False
    for index, line in enumerate(lines):
        if re.fullmatch(r"(?:\w+ ){1,8}об исполнении поручения", line):
            order_reference_title = True
            nearby = " ".join(" ".join(lines[index + 1:index + 9]).split())
            if "уведомляет об исполнении поручения" in nearby:
                order_near_confirmation = True
    order_depot_confirmation = (order_reference_title and order_near_confirmation
                                and has_depot and has_securities and has_deals
                                and not has_report and not has_cash_or_movement
                                and not has_depot_movement)
    order = (order_title and order_confirmation) or order_depot_confirmation
    if trace is not None:
        trace.update(notice_phrase=notice_phrase, notice_type=notice_type,
                     notice_message=notice_message, notice_extended=notice,
                     order_title_extended=order_title, order_confirmation=order_confirmation,
                     order_reference_title=order_reference_title,
                     order_near_confirmation=order_near_confirmation,
                     order_depot_confirmation=order_depot_confirmation,
                     order_extended=order)
    if notice and order:
        return None, 0.0, "CATEGORY_EVIDENCE_INSUFFICIENT"
    if notice:
        return None, 0.0, "CORPORATE_ACTION_NOTIFICATION"
    if order:
        return None, 0.0, "ORDER_EXECUTION_REPORT"
    return None, 0.0, "CATEGORY_EVIDENCE_INSUFFICIENT"


def classify_document(
    extracted_text: Optional[str],
    document_bytes: bytes,
    *,
    filename: str = "",
    mime_type: str = "",
    subject: str = "",
) -> ClassificationResult:
    """Classify extracted content; filename, MIME and subject never select a category."""
    del filename, mime_type, subject
    sha256 = hashlib.sha256(document_bytes).hexdigest()
    text = extracted_text or ""
    category, confidence, category_reason = _category_from_content(text) if text else (None, 0.0, "TEXT_NOT_AVAILABLE")

    accounts: list[str] = []
    seen_accounts: set[str] = set()
    primary_field_present = bool(_PRIMARY_ACCOUNT_LABEL_RE.search(text))
    account_pattern = _PRIMARY_ACCOUNT_RE if primary_field_present else _ACCOUNT_RE
    for match in account_pattern.finditer(text):
        value = match.group("account").strip(" .,:;")
        key = value.casefold()
        if key not in seen_accounts:
            seen_accounts.add(key)
            accounts.append(value)

    raw_periods = [(match.group(1), match.group(2)) for match in _PERIOD_RE.finditer(text)]
    parsed_periods = [(_parse_date(start), _parse_date(end)) for start, end in raw_periods]
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    reasons: list[str] = []
    if parsed_periods:
        if any(start is None or end is None for start, end in parsed_periods):
            reasons.append("PERIOD_DATE_INVALID")
        elif len(set(parsed_periods)) > 1:
            reasons.append("MULTIPLE_REPORTING_PERIODS")
        else:
            period_start, period_end = parsed_periods[0]
            if date.fromisoformat(period_start) > date.fromisoformat(period_end):
                reasons.append("PERIOD_RANGE_REVERSED")

    report_date, report_date_ambiguous = _unique_dates(_REPORT_DATE_RE, text)
    generated_date, generated_date_ambiguous = _unique_dates(_GENERATED_DATE_RE, text)
    if report_date_ambiguous:
        reasons.append("MULTIPLE_REPORT_DATES")
    if generated_date_ambiguous:
        reasons.append("MULTIPLE_GENERATION_DATES")
    if category_reason:
        reasons.append(category_reason)
    if not accounts:
        reasons.append("ACCOUNT_NOT_FOUND")

    review_reasons = {
        "PERIOD_DATE_INVALID", "PERIOD_RANGE_REVERSED", "MULTIPLE_REPORTING_PERIODS",
        "MULTIPLE_REPORT_DATES", "MULTIPLE_GENERATION_DATES",
    }
    status = "CLASSIFIED"
    if category is None or not accounts or any(reason in review_reasons for reason in reasons):
        status = "NEEDS_REVIEW"
    return ClassificationResult(
        category=category,
        account_ids=tuple(accounts),
        period_start=period_start,
        period_end=period_end,
        report_date=report_date,
        generated_date=generated_date,
        sha256=sha256,
        confidence=confidence,
        status=status,
        reasons=tuple(dict.fromkeys(reasons)),
    )
