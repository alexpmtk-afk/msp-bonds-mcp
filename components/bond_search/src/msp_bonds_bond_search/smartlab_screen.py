"""Read-only paginated Smart-Lab listing parser and conservative first-pass screen.

Only listing pages are fetched. Security detail pages are never requested.
Smart-Lab ratings remain preliminary and are never represented as official proof.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import re
from typing import Callable, Mapping
from urllib.parse import urljoin, urlsplit

from .discovery import (
    FetchDiagnostic, SourceUnavailable, _ISIN, _fetch_smartlab, safe_get,
    smartlab_robots_allow,
)
from .policy import SelectionPolicy

SMARTLAB_LISTING_URL = "https://smart-lab.ru/q/bonds/"
_PAGE_SIZE = 100
_MAX_PAGE_REQUESTS = 500
_MAX_UNIQUE_ITEMS = 1000
_MISSING = {"", "-", "—", "–", "n/a", "нет", "не указано"}

_FIELD_ALIASES: dict[str, frozenset[str]] = {
    "instrument_type": frozenset({"тип", "типинструмента", "типоблигации", "видинструмента"}),
    "issuer_country": frozenset({"странаэмитента"}),
    "currency": frozenset({"валюта", "валютаноминала", "валютаинструмента"}),
    "years_to_maturity": frozenset({"летдопогаш", "летдопогашения", "летдопогашен"}),
    "reported_yield_percent": frozenset({"доходн", "доходность", "доходность%"}),
    "annual_coupon_yield_percent": frozenset({"годкупдох", "годоваякупдоходность"}),
    "last_coupon_yield_percent": frozenset({"купдохпосл", "купдоходностьпослед"}),
    "rating_preliminary": frozenset({"рейтинг"}),
    "g_spread_bp": frozenset({"gспред", "gspread"}),
    "volume_mln_rub": frozenset({"объеммлнруб", "объеммлнруб"}),
    "coupon_rub": frozenset({"купонруб"}),
    "coupon_frequency_per_year": frozenset({"частотаразвгод", "частотавгод"}),
    "accrued_interest_rub": frozenset({"нкдр", "нкдруб"}),
    "duration_years": frozenset({"дюрялет", "дюрациялет"}),
    "price_percent_of_nominal": frozenset({"цена"}),
    "next_coupon_date": frozenset({"датакупона"}),
    "placement_date": frozenset({"размещение"}),
    "maturity_date": frozenset({"погашение"}),
    "offer_date": frozenset({"оферта"}),
}
_DECIMAL_FIELDS = frozenset({
    "years_to_maturity", "reported_yield_percent", "annual_coupon_yield_percent",
    "last_coupon_yield_percent", "g_spread_bp", "volume_mln_rub", "coupon_rub",
    "coupon_frequency_per_year", "accrued_interest_rub", "duration_years",
    "price_percent_of_nominal",
})
_DATE_FIELDS = frozenset({"next_coupon_date", "placement_date", "maturity_date", "offer_date"})
_STRING_FIELDS = frozenset({
    "rating_preliminary", "instrument_type", "issuer_country", "currency",
})
_OPTIONAL_SCREEN_COLUMNS = frozenset({"instrument_type", "issuer_country", "currency"})
_RATING_ORDER = (
    "AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
    "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D",
)


@dataclass(frozen=True)
class SmartLabBond:
    isin: str
    name: str
    listing_url: str
    fetched_at: datetime
    fields: Mapping[str, Decimal | date | str | None]
    other_fields: Mapping[str, str]
    missing_columns: tuple[str, ...] = ()
    source_row_number: int | None = None

    def to_dict(self) -> dict[str, object]:
        values: dict[str, object] = {}
        for key, value in self.fields.items():
            values[key] = value.isoformat() if isinstance(value, date) else (
                str(value) if isinstance(value, Decimal) else value
            )
        return {
            "isin": self.isin,
            "name": self.name,
            "source": "Smart-Lab",
            "source_url": self.listing_url,
            "fetched_at": self.fetched_at.isoformat(),
            "source_row_number": self.source_row_number,
            "fields": values,
            "other_fields": dict(self.other_fields),
            "missing_columns": list(self.missing_columns),
        }


@dataclass(frozen=True)
class PreliminaryAssessment:
    status: str
    reasons: tuple[str, ...]
    decision_blockers: tuple[str, ...]
    selection_signals: tuple[str, ...]
    needs_verification: tuple[str, ...]
    preferences: tuple[str, ...]
    policy_fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "decision_blockers": list(self.decision_blockers),
            "selection_signals": list(self.selection_signals),
            "needs_verification": list(self.needs_verification),
            "preferences": list(self.preferences),
            "policy_fingerprint": self.policy_fingerprint,
        }


@dataclass(frozen=True)
class SmartLabSearchResult:
    bonds: tuple[SmartLabBond, ...]
    status: str
    requested_max_items: int | None
    pages_fetched: int
    duplicate_rows: int
    rows_without_isin: int
    schema_warnings: tuple[str, ...]
    retrieved_at: datetime
    source_error: str | None = None
    source_diagnostics: dict[str, object] | None = None
    collect_all: bool = False
    integrity_issues: tuple[str, ...] = ()
    row_sequence_verified: bool = False
    sort_signature: str | None = None


class _ListingHTML(HTMLParser):
    """Capture table rows and hrefs without loading any linked security pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[list[str], list[str]]] = []
        self.links: list[str] = []
        self._in_row = False
        self._in_cell = False
        self._cells: list[str] = []
        self._row_links: list[str] = []
        self._cell_text: list[str] = []
        self._cell_colspan = 1

    def handle_starttag(self, tag: str, attrs) -> None:
        attr = dict(attrs)
        if tag == "tr":
            self._in_row = True
            self._cells, self._row_links = [], []
        elif tag in ("td", "th") and self._in_row:
            self._in_cell = True
            self._cell_text = []
            try:
                self._cell_colspan = max(1, min(int(attr.get("colspan", "1")), 20))
            except (TypeError, ValueError):
                self._cell_colspan = 1
        elif tag == "a":
            href = attr.get("href")
            if isinstance(href, str):
                self.links.append(href)
                if self._in_row:
                    self._row_links.append(href)

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_cell:
            value = " ".join(" ".join(self._cell_text).split())
            self._cells.extend([value] * self._cell_colspan)
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            self.rows.append((self._cells[:], self._row_links[:]))
            self._in_row = False
            self._in_cell = False


def _header_key(value: str) -> str:
    value = value.casefold().replace("ё", "е")
    return "".join(ch for ch in value if ch.isalnum())


def _header_map(rows: list[tuple[list[str], list[str]]]) -> tuple[int, dict[str, int], tuple[str, ...]]:
    for row_index, (cells, _) in enumerate(rows):
        keys = [_header_key(cell) for cell in cells]
        if "имя" not in keys:
            continue
        positions = {key: index for index, key in enumerate(keys) if key}
        mapped = {"name": positions["имя"]}
        for field, aliases in _FIELD_ALIASES.items():
            match = next((positions[alias] for alias in aliases if alias in positions), None)
            if match is not None:
                mapped[field] = match
        warnings = tuple(
            field for field in _FIELD_ALIASES
            if field not in mapped and field not in _OPTIONAL_SCREEN_COLUMNS
        )
        return row_index, mapped, warnings
    raise SourceUnavailable(
        "Smart-Lab listing table header was not recognized",
        code="HTML_SCHEMA_CHANGED", phase="parse",
    )


def _missing(value: str) -> bool:
    return value.strip().casefold() in _MISSING


def _decimal(value: str) -> Decimal | None:
    if _missing(value):
        return None
    cleaned = value.replace("\u00a0", "").replace(" ", "").replace("%", "").replace(",", ".")
    cleaned = cleaned.replace("−", "-")
    try:
        number = Decimal(cleaned)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _date(value: str) -> date | None:
    if _missing(value):
        return None
    for pattern in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            pass
    return None


def _extract_isin(links: list[str], cells: list[str]) -> str | None:
    found = {match.group(0) for text in (*links, *cells) for match in _ISIN.finditer(text.upper())}
    return next(iter(found)) if len(found) == 1 else None


def parse_smartlab_listing(
    html: str,
    *,
    listing_url: str = SMARTLAB_LISTING_URL,
    fetched_at: datetime | None = None,
    max_items: int | None = 1000,
) -> tuple[list[SmartLabBond], tuple[str, ...], list[str]]:
    """Parse one listing page, returning records, schema warnings, and all hrefs."""
    if max_items is not None and (
        not isinstance(max_items, int) or not 1 <= max_items <= _MAX_UNIQUE_ITEMS
    ):
        raise ValueError("max_items must be None or 1..1000")
    timestamp = fetched_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("fetched_at must include a timezone")
    parser = _ListingHTML()
    parser.feed(html)
    header_index, columns, warnings = _header_map(parser.rows)
    row_number_index = 0
    header_keys = [_header_key(cell) for cell in parser.rows[header_index][0]]
    for index, key in enumerate(header_keys):
        if key in {"номер", "n"}:
            row_number_index = index
            break
    known_indices = {row_number_index, columns.get("name"), *columns.values()}
    bonds: list[SmartLabBond] = []
    seen: set[str] = set()
    for cells, links in parser.rows[header_index + 1:]:
        isin = _extract_isin(links, cells)
        if not isin or isin in seen:
            continue
        name_index = columns["name"]
        name = cells[name_index].strip() if name_index < len(cells) else ""
        if not name:
            continue
        row_number = None
        if row_number_index < len(cells) and cells[row_number_index].strip().isdigit():
            row_number = int(cells[row_number_index].strip())
        seen.add(isin)
        fields: dict[str, Decimal | date | str | None] = {}
        missing_columns: list[str] = []
        for field in (*_DECIMAL_FIELDS, *_DATE_FIELDS, *_STRING_FIELDS):
            index = columns.get(field)
            raw = cells[index].strip() if index is not None and index < len(cells) else ""
            if field not in columns:
                if field not in _OPTIONAL_SCREEN_COLUMNS:
                    missing_columns.append(field)
                value = None
            elif field in _DECIMAL_FIELDS:
                value = _decimal(raw)
            elif field in _DATE_FIELDS:
                value = _date(raw)
            else:
                value = None if _missing(raw) else raw.upper().replace("−", "-").replace("–", "-")
            fields[field] = value

        other: dict[str, str] = {}
        for index, header in enumerate(parser.rows[header_index][0]):
            if index in known_indices or not header.strip() or index >= len(cells):
                continue
            raw = cells[index].strip()
            if raw and raw not in {"+", "-", "—", "–"}:
                other[header.strip()] = raw
        bonds.append(SmartLabBond(
            isin=isin,
            name=name,
            listing_url=listing_url,
            fetched_at=timestamp.astimezone(timezone.utc),
            fields=fields,
            other_fields=other,
            missing_columns=tuple(missing_columns),
            source_row_number=row_number,
        ))
        if max_items is not None and len(bonds) >= max_items:
            break
    return bonds, warnings, parser.links


def _page_number(url: str) -> int | None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "smart-lab.ru" or parts.username or parts.password:
        return None
    if parts.path.rstrip("/") == "/q/bonds":
        return 1
    match = re.fullmatch(r"/q/bonds/order_by_[a-z0-9_]+/desc/page(\d+)", parts.path.rstrip("/"))
    return int(match.group(1)) if match else None


def _pagination_links(links: list[str], base_url: str) -> dict[int, str]:
    pages: dict[int, str] = {}
    for href in links:
        absolute = urljoin(base_url, href)
        number = _page_number(absolute)
        if number is not None:
            pages.setdefault(number, absolute)
    return pages


def _pagination_sort_signature(url: str) -> str | None:
    parts = urlsplit(url)
    match = re.fullmatch(
        r"/q/bonds/order_by_([a-z0-9_]+)/([a-z]+)/page\d+/?",
        parts.path,
        re.I,
    )
    if not match:
        return None
    return f"{match.group(1).lower()}:{match.group(2).lower()}"


def _fetch_with_diagnostics(
    url: str,
    *,
    step: str,
    fetch: Callable[[str], bytes],
    request_log: list[dict[str, object]],
) -> tuple[bytes, list[dict[str, object]]]:
    start = len(request_log)

    def record(item: FetchDiagnostic) -> None:
        request_log.append({
            "step": step,
            "url": item.url or url,
            "attempt": item.attempt,
            "phase": item.phase,
            "status": "FAILED" if item.reason_code else "FETCHED",
            "http_status": item.http_status,
            "response_bytes": item.response_bytes,
            "elapsed_ms": round(item.elapsed_seconds * 1000, 1),
            "error_code": item.reason_code or None,
            "content_length": item.content_length,
            "transfer_encoding": item.transfer_encoding,
            "content_encoding": item.content_encoding,
            "body_framing": item.body_framing,
            "body_complete": item.body_complete,
            "wire_bytes": item.wire_bytes,
            "curl_exit_code": item.curl_exit_code,
            "cache_status": item.cache_status or None,
            "cache_age_seconds": item.cache_age_seconds,
            "content_fetched_at": item.content_fetched_at or None,
        })

    try:
        payload = _fetch_smartlab(url, fetch, observer=record)
    except SourceUnavailable as exc:
        if len(request_log) == start:
            request_log.append({
                "step": step, "url": exc.source_url or url,
                "attempt": exc.attempts, "phase": exc.phase or "request",
                "status": "FAILED", "http_status": exc.http_status,
                "response_bytes": exc.response_bytes,
                "elapsed_ms": round(exc.elapsed_seconds * 1000, 1),
                "error_code": exc.code,
            })
        exc.source_url = exc.source_url or url
        exc.request_diagnostics = tuple(request_log[start:])
        raise
    return payload, request_log[start:]


def _attempt_trace(request_log: list[dict[str, object]]) -> str:
    return ";".join(
        f"{item.get('step')}_try{item.get('attempt')}={item.get('error_code')}@{item.get('url')}"
        f"(bytes={item.get('response_bytes')},cl={item.get('content_length')},"
        f"te={item.get('transfer_encoding') or 'none'},"
        f"ce={item.get('content_encoding') or 'identity'},"
        f"framing={item.get('body_framing') or 'unknown'},"
        f"complete={item.get('body_complete')},"
        f"wire={item.get('wire_bytes')},curl={item.get('curl_exit_code')},"
        f"cache={item.get('cache_status') or 'none'})"
        for item in request_log
        if item.get("error_code")
    )


def _robots_trace(robots: dict[str, object]) -> str:
    return (
        f"robots={robots.get('status')},http={robots.get('http_status')},"
        f"bytes={robots.get('response_bytes')}@{robots.get('url')}"
    )


def discover_smartlab_pages(
    *,
    fetch: Callable[[str], bytes] = safe_get,
    now: datetime | None = None,
    max_items: int | None = 100,
) -> SmartLabSearchResult:
    """Fetch a bounded sample or follow Smart-Lab pagination to its stated end."""
    if max_items is not None and (
        not isinstance(max_items, int) or not 1 <= max_items <= _MAX_UNIQUE_ITEMS
    ):
        raise ValueError("max_items must be None or 1..1000")
    collect_all = max_items is None
    request_started_at = now or datetime.now(timezone.utc)
    if request_started_at.tzinfo is None:
        raise ValueError("now must include a timezone")

    all_bonds: dict[str, SmartLabBond] = {}
    warnings: list[str] = []
    duplicate_rows = 0
    rows_without_isin = 0
    pages_fetched = 0
    current_url = SMARTLAB_LISTING_URL
    visited: set[str] = set()
    status = "COMPLETE"
    source_error: str | None = None
    integrity_issues: list[str] = []
    expected_next_row = 1
    row_sequence_verified = True
    observed_sort_signature: str | None = None
    request_log: list[dict[str, object]] = []
    listing_pages: list[dict[str, object]] = []
    robots_url = "https://smart-lab.ru/robots.txt"
    robots_diagnostics: dict[str, object] = {
        "url": robots_url,
        "status": "NOT_CHECKED",
        "http_status": None,
        "response_bytes": 0,
        "attempts": [],
        "page_checks": [],
    }
    try:
        robots_raw, robots_attempts = _fetch_with_diagnostics(
            robots_url, step="robots_fetch", fetch=fetch, request_log=request_log,
        )
        robots_diagnostics["attempts"] = robots_attempts
        robots_diagnostics["response_bytes"] = len(robots_raw)
        robots_diagnostics["http_status"] = next(
            (event.get("http_status") for event in reversed(robots_attempts)
             if event.get("status") == "FETCHED"),
            None,
        )
        robots_diagnostics["status"] = "FETCHED"
    except SourceUnavailable as exc:
        robots_diagnostics["status"] = "FAILED"
        robots_diagnostics["attempts"] = list(exc.request_diagnostics)
        exc.source_url = exc.source_url or robots_url
        attempt_trace = _attempt_trace(request_log)
        exc.diagnostic_context = _robots_trace(robots_diagnostics) + (
            f";{attempt_trace}" if attempt_trace else ""
        )
        exc.request_diagnostics = tuple(request_log)
        raise

    while len(listing_pages) < _MAX_PAGE_REQUESTS and (
        collect_all or len(all_bonds) < max_items
    ):
        if current_url in visited:
            status = "INCOMPLETE_PAGINATION_LOOP"
            break
        page_number = _page_number(current_url) or len(listing_pages) + 1
        try:
            allowed = smartlab_robots_allow(
                fetch, page_url=current_url, robots_payload=robots_raw,
            )
        except SourceUnavailable as exc:
            robots_diagnostics["status"] = "FAILED"
            robots_diagnostics["page_checks"].append({
                "page": page_number, "url": current_url, "status": "CHECK_FAILED",
            })
            exc.source_url = exc.source_url or current_url
            exc.diagnostic_context = (
                f"{_robots_trace(robots_diagnostics)};"
                f"robots_page{page_number}=CHECK_FAILED@{current_url}"
            )
            exc.request_diagnostics = tuple(request_log)
            if pages_fetched == 0:
                raise
            status = "PARTIAL_SOURCE_FAILURE"
            source_error = exc.diagnostic_summary
            break
        check = {
            "page": page_number, "url": current_url,
            "status": "ALLOWED" if allowed else "DISALLOWED",
        }
        robots_diagnostics["page_checks"].append(check)
        if not allowed:
            robots_diagnostics["status"] = "DISALLOWED"
            listing_pages.append({
                "page": page_number, "url": current_url,
                "status": "BLOCKED_BY_ROBOTS", "attempts": [],
                "http_status": None, "response_bytes": 0,
            })
            exc = SourceUnavailable(
                "Smart-Lab robots.txt disallows this listing page",
                code="ROBOTS_DISALLOWED", phase="robots", source_url=current_url,
            )
            exc.diagnostic_context = (
                f"{_robots_trace(robots_diagnostics)};"
                f"robots_page{page_number}=DISALLOWED@{current_url}"
            )
            exc.request_diagnostics = tuple(request_log)
            if pages_fetched == 0:
                raise exc
            status = "PARTIAL_SOURCE_FAILURE"
            source_error = exc.diagnostic_summary
            break
        robots_diagnostics["status"] = "ALLOWED"
        visited.add(current_url)
        page_diagnostic: dict[str, object] = {
            "page": page_number, "url": current_url, "status": "REQUESTING",
            "attempts": [], "http_status": None, "response_bytes": 0,
        }
        listing_pages.append(page_diagnostic)
        try:
            raw, page_attempts = _fetch_with_diagnostics(
                current_url, step="listing_page_fetch", fetch=fetch, request_log=request_log,
            )
            page_diagnostic["attempts"] = page_attempts
            page_diagnostic["http_status"] = next(
                (event.get("http_status") for event in reversed(page_attempts)
                 if event.get("status") == "FETCHED"),
                None,
            )
            page_diagnostic["response_bytes"] = len(raw)
            page_diagnostic["status"] = "FETCHED"
            completed_transport = next(
                (event for event in reversed(page_attempts) if event.get("status") == "FETCHED"),
                None,
            )
            if completed_transport and completed_transport.get("cache_status"):
                page_diagnostic["cache_status"] = completed_transport.get("cache_status")
                page_diagnostic["cache_age_seconds"] = completed_transport.get("cache_age_seconds")
        except SourceUnavailable as exc:
            page_diagnostic["attempts"] = list(exc.request_diagnostics)
            page_diagnostic["http_status"] = exc.http_status
            page_diagnostic["response_bytes"] = exc.response_bytes
            page_diagnostic["status"] = "FETCH_FAILED"
            exc.source_url = exc.source_url or current_url
            exc.diagnostic_context = (
                f"{_robots_trace(robots_diagnostics)};"
                f"page{page_number}=FETCH_FAILED,http={exc.http_status},"
                f"bytes={exc.response_bytes}@{current_url}"
            )
            attempt_trace = _attempt_trace(request_log)
            if attempt_trace:
                exc.diagnostic_context += f";{attempt_trace}"
            exc.request_diagnostics = tuple(request_log)
            if pages_fetched == 0:
                raise
            status = "PARTIAL_SOURCE_FAILURE"
            source_error = exc.diagnostic_summary
            break
        page_fetched_at = request_started_at if now is not None else datetime.now(timezone.utc)
        fetched_at_value = next(
            (event.get("content_fetched_at") for event in reversed(page_attempts)
             if event.get("status") == "FETCHED" and event.get("content_fetched_at")),
            None,
        )
        if isinstance(fetched_at_value, str):
            try:
                parsed_fetched_at = datetime.fromisoformat(fetched_at_value)
                if parsed_fetched_at.tzinfo is not None:
                    page_fetched_at = parsed_fetched_at.astimezone(timezone.utc)
            except ValueError:
                pass
        try:
            html = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            page_diagnostic["status"] = "DECODE_FAILED"
            exc = SourceUnavailable(
                "Smart-Lab response is not valid UTF-8", code="INVALID_ENCODING",
                phase="read", source_url=current_url,
            )
            exc.diagnostic_context = (
                f"{_robots_trace(robots_diagnostics)};"
                f"page{page_number}=DECODE_FAILED@{current_url}"
            )
            exc.request_diagnostics = tuple(request_log)
            if pages_fetched == 0:
                raise exc from None
            status = "PARTIAL_SOURCE_FAILURE"
            source_error = exc.diagnostic_summary
            break
        try:
            page_bonds, page_warnings, links = parse_smartlab_listing(
                html, listing_url=current_url, fetched_at=page_fetched_at,
                max_items=None,
            )
        except SourceUnavailable as exc:
            page_diagnostic["status"] = "PARSE_FAILED"
            exc.source_url = exc.source_url or current_url
            exc.diagnostic_context = (
                f"{_robots_trace(robots_diagnostics)};"
                f"page{page_number}=PARSE_FAILED@{current_url}"
            )
            exc.request_diagnostics = tuple(request_log)
            if pages_fetched == 0:
                raise
            status = "PARTIAL_SCHEMA_FAILURE"
            source_error = exc.diagnostic_summary
            break
        pages_fetched += 1
        page_diagnostic["status"] = "PARSED"
        for warning in page_warnings:
            if warning not in warnings:
                warnings.append(warning)
        row_count, page_missing_isin, page_duplicates, row_numbers = _listing_row_stats(html)
        rows_without_isin += page_missing_isin
        duplicate_rows += page_duplicates
        page_diagnostic["data_rows"] = row_count
        page_diagnostic["first_row_number"] = row_numbers[0] if row_numbers else None
        page_diagnostic["last_row_number"] = row_numbers[-1] if row_numbers else None
        if row_count:
            expected_rows = list(range(expected_next_row, expected_next_row + row_count))
            if row_numbers != expected_rows:
                row_sequence_verified = False
                issue = f"ROW_NUMBER_SEQUENCE_MISMATCH_PAGE_{page_number}"
                if issue not in integrity_issues:
                    integrity_issues.append(issue)
            if row_numbers:
                expected_next_row = row_numbers[-1] + 1
            else:
                row_sequence_verified = False
                issue = f"ROW_NUMBERS_MISSING_PAGE_{page_number}"
                if issue not in integrity_issues:
                    integrity_issues.append(issue)
        else:
            row_sequence_verified = False
            issue = f"EMPTY_OR_UNNUMBERED_LISTING_PAGE_{page_number}"
            if issue not in integrity_issues:
                integrity_issues.append(issue)

        available = _pagination_links(links, current_url)
        page_sort_signatures = {
            signature for target in available.values()
            if (signature := _pagination_sort_signature(target)) is not None
        }
        url_sort_signature = _pagination_sort_signature(current_url)
        page_sort_signature = url_sort_signature
        if page_sort_signature is None and len(page_sort_signatures) == 1:
            page_sort_signature = next(iter(page_sort_signatures))
        elif page_sort_signature is None and len(page_sort_signatures) > 1:
            issue = f"SORT_SIGNATURE_AMBIGUOUS_PAGE_{page_number}"
            if issue not in integrity_issues:
                integrity_issues.append(issue)
        elif page_sort_signature is None and not available:
            page_sort_signature = "default"
        if (
            url_sort_signature is not None
            and page_sort_signatures
            and page_sort_signatures != {url_sort_signature}
        ):
            issue = f"SORT_SIGNATURE_CHANGED_PAGE_{page_number}"
            if issue not in integrity_issues:
                integrity_issues.append(issue)
        if page_sort_signature is not None:
            if observed_sort_signature is None:
                observed_sort_signature = page_sort_signature
            elif observed_sort_signature != page_sort_signature:
                issue = f"SORT_SIGNATURE_CHANGED_PAGE_{page_number}"
                if issue not in integrity_issues:
                    integrity_issues.append(issue)
        page_diagnostic["sort_signature"] = page_sort_signature
        page_diagnostic["pagination_numbers"] = sorted(available)

        previous_isins = set(all_bonds)
        for bond in page_bonds:
            if bond.isin in all_bonds:
                if bond.isin in previous_isins:
                    duplicate_rows += 1
                continue
            elif collect_all or len(all_bonds) < max_items:
                all_bonds[bond.isin] = bond

        if not collect_all and len(all_bonds) >= max_items:
            status = "LIMIT_REACHED"
            break

        current_number = _page_number(current_url) or pages_fetched
        next_number = min((number for number in available if number > current_number), default=None)
        if next_number is None:
            current_page_is_listed = current_number in available
            max_announced_page = max(available, default=current_number)
            if max_announced_page > current_number:
                status = "PAGINATION_GAP"
                issue = f"NEXT_PAGE_LINK_MISSING_AFTER_{current_number}"
                if issue not in integrity_issues:
                    integrity_issues.append(issue)
            elif row_count >= _PAGE_SIZE and not available:
                # Full-looking pages without pagination evidence cannot safely be called complete.
                status = "PAGINATION_UNKNOWN"
                issue = f"PAGINATION_END_UNCONFIRMED_AFTER_{current_number}"
                if issue not in integrity_issues:
                    integrity_issues.append(issue)
            break
        if next_number != current_number + 1:
            status = "PAGINATION_GAP"
            issue = f"PAGINATION_SEQUENCE_GAP_{current_number}_TO_{next_number}"
            if issue not in integrity_issues:
                integrity_issues.append(issue)
            break
        if pages_fetched >= _MAX_PAGE_REQUESTS:
            status = "PAGE_REQUEST_LIMIT_REACHED"
            integrity_issues.append("PAGE_REQUEST_SAFETY_LIMIT_REACHED")
            break
        current_url = available[next_number]

    if rows_without_isin and "LISTING_ROWS_WITHOUT_ISIN" not in integrity_issues:
        integrity_issues.append("LISTING_ROWS_WITHOUT_ISIN")
    if duplicate_rows and "DUPLICATE_ISIN_ROWS_DETECTED" not in integrity_issues:
        integrity_issues.append("DUPLICATE_ISIN_ROWS_DETECTED")
    if status == "COMPLETE":
        if rows_without_isin:
            status = "PARTIAL_IDENTIFIER_COVERAGE" if all_bonds else "NO_ISIN_IDENTIFIERS"
        elif duplicate_rows:
            status = "DUPLICATE_ISIN_ROWS_DETECTED"
        elif integrity_issues:
            status = "INTEGRITY_CHECK_FAILED"
    # Count rows that lack identifiers only when a page was parsed. The page-size
    # signal is kept separate because Smart-Lab may use decorative table rows.
    return SmartLabSearchResult(
        bonds=tuple(all_bonds.values()), status=status, requested_max_items=max_items,
        pages_fetched=pages_fetched,
        duplicate_rows=duplicate_rows, rows_without_isin=rows_without_isin,
        schema_warnings=tuple(warnings),
        retrieved_at=(request_started_at if now is not None else datetime.now(timezone.utc)).astimezone(timezone.utc),
        source_error=source_error,
        source_diagnostics={
            "robots": robots_diagnostics,
            "listing_pages": listing_pages,
            "request_attempts": request_log,
            "collection": {
                "mode": "ALL_AVAILABLE_PAGES" if collect_all else "LIMITED_SAMPLE",
                "page_safety_limit": _MAX_PAGE_REQUESTS,
                "row_sequence_verified": row_sequence_verified,
                "sort_signature": observed_sort_signature,
                "integrity_issues": list(dict.fromkeys(integrity_issues)),
            },
        },
        collect_all=collect_all,
        integrity_issues=tuple(dict.fromkeys(integrity_issues)),
        row_sequence_verified=row_sequence_verified,
        sort_signature=observed_sort_signature,
    )


def _ListingHTMLRows(html: str) -> list[tuple[list[str], list[str]]]:
    parser = _ListingHTML()
    parser.feed(html)
    return parser.rows


def _listing_row_stats(html: str) -> tuple[int, int, int, list[int]]:
    """Count table rows and collect the displayed sequential row numbers."""
    rows = _ListingHTMLRows(html)
    header_index, columns, _ = _header_map(rows)
    name_index = columns["name"]
    data_rows = missing_isin = duplicates = 0
    row_numbers: list[int] = []
    seen: set[str] = set()
    for cells, links in rows[header_index + 1:]:
        if name_index >= len(cells) or not cells[name_index].strip():
            continue
        if not cells or not cells[0].strip().isdigit():
            continue
        data_rows += 1
        row_numbers.append(int(cells[0].strip()))
        isin = _extract_isin(links, cells)
        if isin is None:
            missing_isin += 1
        elif isin in seen:
            duplicates += 1
        else:
            seen.add(isin)
    return data_rows, missing_isin, duplicates, row_numbers


def _canonical_preliminary_rating(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    grade = value.upper().replace("Ё", "Е").replace("−", "-").replace("–", "-").strip()
    grade = grade.replace("А", "A").replace("В", "B").replace("С", "C")
    return grade if grade in _RATING_ORDER else None


def _remaining_years(bond: SmartLabBond) -> Decimal | None:
    maturity = bond.fields.get("maturity_date")
    if isinstance(maturity, date):
        return Decimal((maturity - bond.fetched_at.date()).days) / Decimal("365.2425")
    years = bond.fields.get("years_to_maturity")
    return years if isinstance(years, Decimal) else None


def assess_preliminary(
    bond: SmartLabBond,
    policy: SelectionPolicy,
    *,
    issuer_country: str | None = None,
    currency: str | None = None,
    instrument_type: str | None = None,
    official_rating: str | None = None,
    official_rating_confirmed: bool = False,
    corporate_instrument_confirmed: bool = False,
    future_coupon_terms_confirmed: bool = False,
    liquidity_reviewed: bool = False,
    issuer_bbb_units_total: int | None = None,
) -> PreliminaryAssessment:
    """Screen the Smart-Lab evidence first; keep official due diligence separate."""
    reject: list[str] = []
    blockers: list[str] = []
    signals: list[str] = []
    verify: list[str] = []
    preferences: list[str] = []

    country_value = issuer_country or (
        str(bond.fields.get("issuer_country")) if bond.fields.get("issuer_country") else None
    )
    currency_value = currency or (
        str(bond.fields.get("currency")) if bond.fields.get("currency") else None
    )
    type_value = instrument_type or (
        str(bond.fields.get("instrument_type")) if bond.fields.get("instrument_type") else None
    )

    if country_value is None:
        verify.append("issuer_country_confirmation_from_official_record")
    else:
        verify.append("issuer_country_confirmation_from_official_record")
        country = re.sub(r"[^A-ZА-Я0-9]", "", country_value.upper().replace("Ё", "Е"))
        if country not in {"RU", "RUS", "RUSSIA", "РОССИЯ", "РФ", "РОССИЙСКАЯФЕДЕРАЦИЯ", "RUSSIANFEDERATION"}:
            reject.append("issuer_not_russian")
        else:
            signals.append("preliminary_source_country_is_russian;official_confirmation_pending")

    if currency_value is None:
        verify.append("ruble_currency_confirmation_from_issue_record")
    else:
        verify.append("ruble_currency_confirmation_from_issue_record")
        currency_code = (
            "RUB" if "₽" in currency_value else
            re.sub(r"[^A-ZА-Я0-9]", "", currency_value.upper().replace("Ё", "Е"))
        )
        if currency_code not in {"RUB", "RUR", "РУБ", "РУБЛЬ", "РУБЛЕЙ", "РОССИЙСКИЙРУБЛЬ", "₽"}:
            reject.append("currency_not_rub")
        else:
            signals.append("preliminary_currency_is_ruble;official_confirmation_pending")

    if type_value:
        verify.append("instrument_classification_from_official_security_record")
        normalized_type = re.sub(r"[^A-ZА-Я0-9]", "", type_value.upper().replace("Ё", "Е"))
        if any(token in normalized_type for token in (
            "ОФЗ", "OFZ", "ФЕДЕРАЛЬН", "МУНИЦИПАЛЬН", "ГОСУДАРСТВЕНН", "GOVERNMENT",
        )):
            reject.append("instrument_not_corporate_bond")
        elif "КОРПОРАТ" in normalized_type or "CORPORATE" in normalized_type:
            signals.append("preliminary_instrument_type_is_corporate;official_confirmation_pending")
        else:
            blockers.append("instrument_type_is_ambiguous_in_smartlab_data")
            verify.append("instrument_classification_from_official_security_record")
    elif corporate_instrument_confirmed:
        signals.append("corporate_instrument_type_confirmed")
    else:
        name_key = re.sub(r"[^A-ZА-Я0-9]", "", bond.name.upper().replace("Ё", "Е"))
        if "ОФЗ" in name_key or "OFZ" in name_key or "МУНИЦИПАЛЬН" in name_key:
            reject.append("instrument_not_corporate_bond")
        else:
            signals.append("listed_in_smartlab_bond_universe;instrument_classification_pending")
            verify.append("instrument_classification_from_official_security_record")

    shown_rating = bond.fields.get("rating_preliminary")
    rating_text = official_rating if official_rating_confirmed else shown_rating
    grade = _canonical_preliminary_rating(rating_text)
    minimum = _canonical_preliminary_rating(policy.minimum_rating)
    if minimum is None:
        raise ValueError("policy minimum rating is not in the supported rating scale")

    if not official_rating_confirmed:
        verify.append("official_rating_agency_scale_subject_and_action_date_confirmation")
    if grade is None:
        blockers.append("rating_missing_or_unrecognized_for_initial_floor_check")
        if rating_text:
            verify.append("rating_not_mapped_to_the_supported_grade_scale")
        else:
            verify.append("current_rating_from_an_active_agency")
    else:
        below_floor = _RATING_ORDER.index(grade) > _RATING_ORDER.index(minimum)
        if official_rating_confirmed:
            if below_floor:
                reject.append("confirmed_rating_below_approved_minimum")
            else:
                signals.append("official_rating_meets_approved_minimum")
                if _RATING_ORDER.index(grade) <= _RATING_ORDER.index("A-"):
                    preferences.append("A_and_higher_priority;A_minus_is_eligible")
                if grade == "BBB":
                    if issuer_bbb_units_total is None:
                        verify.append("current_bbb_issuer_count_across_accounts")
                    elif issuer_bbb_units_total >= policy.bbb_units_limit:
                        reject.append("approved_bbb_issuer_five_bond_cap_reached")
                elif grade == "BBB+":
                    preferences.append("BBB+ five-bond cap is a working interpretation, not a hard approved rule")
                    if issuer_bbb_units_total is None:
                        verify.append("bbb_plus_issuer_count_owner_review")
                    elif issuer_bbb_units_total >= policy.bbb_units_limit:
                        preferences.append("bbb_plus_working_cap_would_be_reached;owner_review_required")
        else:
            verify.append("official_rating_confirmation_smartlab_rating_is_preliminary")
            if below_floor:
                reject.append("preliminary_rating_below_approved_minimum")
                signals.append("smartlab_preliminary_rating_is_below_floor;excluded_from_initial_screen")
            else:
                signals.append("smartlab_preliminary_rating_meets_or_exceeds_approved_floor")
                if _RATING_ORDER.index(grade) <= _RATING_ORDER.index("A-"):
                    preferences.append("A_and_higher_priority;A_minus_is_eligible")
                if grade in {"BBB", "BBB+"}:
                    verify.append("current_bbb_issuer_count_across_accounts_not_loaded")

    if not future_coupon_terms_confirmed:
        verify.append("future_coupon_terms_and_offer_mechanisms_from_issue_documents")

    years = _remaining_years(bond)
    if years is None:
        verify.append("maturity_horizon_confirmation_from_official_source")
    elif years <= 0:
        reject.append("bond_is_past_or_at_maturity")
    elif years > policy.preferred_maturity_years:
        preferences.append("maturity_over_three_years_is_preference_deviation_not_rejection")
        verify.append("maturity_date_confirmation_from_official_issue_record")
    else:
        signals.append("maturity_within_preferred_horizon")
        verify.append("maturity_date_confirmation_from_official_issue_record")

    if bond.fields.get("reported_yield_percent") is None:
        verify.append("reported_yield_unavailable;no_minimum_yield_threshold_applied")
    else:
        signals.append("reported_yield_available;no_minimum_yield_threshold_applied")
        verify.append("yield_methodology_and_reference_price_confirmation")
    if bond.fields.get("volume_mln_rub") is None:
        verify.append("reported_trading_volume_unavailable")
    else:
        signals.append("reported_volume_is_not_a_liquidity_measure")
        verify.append("reported_volume_period_and_calculation_not_confirmed")

    if bond.fields.get("coupon_rub") is None or bond.fields.get("coupon_frequency_per_year") is None:
        verify.append("coupon_amount_and_frequency_from_issue_terms")
    if bond.fields.get("accrued_interest_rub") is None or bond.fields.get("price_percent_of_nominal") is None:
        verify.append("current_price_and_accrued_interest_confirmation")
    if bond.fields.get("next_coupon_date") is None:
        verify.append("next_coupon_date_confirmation")
    if bond.fields.get("offer_date") is None:
        verify.append("offer_presence_and_conditions_from_issue_documents")
    coupon_conflicts, coupon_checks = _coupon_consistency_flags(bond)
    maturity_conflicts = _maturity_consistency_flags(bond)
    if coupon_conflicts:
        blockers.append("smartlab_coupon_fields_have_incompatible_values")
    if maturity_conflicts:
        blockers.append("smartlab_maturity_fields_have_incompatible_values")
    verify.extend(coupon_checks)
    if maturity_conflicts:
        verify.append("verify_maturity_and_offer_fields_against_issue_documents")
    if bond.fields.get("duration_years") is not None:
        signals.append("duration_is_reported_separately_and_not_used_as_maturity_horizon")
    else:
        verify.append("duration_value_unavailable;not_a_substitute_for_maturity")
    if not liquidity_reviewed:
        verify.append("order_book_spread_depth_and_trade_frequency_not_established_by_listing_volume")
    else:
        signals.append("order_book_liquidity_reviewed_separately_from_reported_volume")

    preferences.append("no_fixed_minimum_yield_or_nominal_limit_applied")
    if reject:
        status = "НЕ ПОДХОДИТ"
    elif blockers:
        status = "ТРЕБУЕТ УТОЧНЕНИЯ"
    else:
        status = "ПРЕДВАРИТЕЛЬНО ПОДХОДИТ"
    return PreliminaryAssessment(
        status=status,
        reasons=tuple(dict.fromkeys(reject)),
        decision_blockers=tuple(dict.fromkeys(blockers)),
        selection_signals=tuple(dict.fromkeys(signals)),
        needs_verification=tuple(dict.fromkeys(verify)),
        preferences=tuple(dict.fromkeys(preferences)),
        policy_fingerprint=policy.snapshot.fingerprint,
    )


def _coupon_consistency_flags(bond: SmartLabBond) -> tuple[list[str], list[str]]:
    coupon = bond.fields.get("coupon_rub")
    frequency = bond.fields.get("coupon_frequency_per_year")
    next_coupon = bond.fields.get("next_coupon_date")
    maturity = bond.fields.get("maturity_date")
    conflicts: list[str] = []
    checks: list[str] = []
    if isinstance(coupon, Decimal) and coupon > 0 and isinstance(frequency, Decimal) and frequency <= 0:
        conflicts.append("positive_coupon_amount_with_zero_or_negative_frequency")
    if isinstance(next_coupon, date) and isinstance(maturity, date) and next_coupon > maturity:
        conflicts.append("next_coupon_date_after_final_maturity")
    if conflicts:
        checks.append("verify_conflicting_listing_fields_against_issue_documents")
    if isinstance(next_coupon, date) and next_coupon < bond.fetched_at.date():
        checks.append("confirm_stale_or_past_next_coupon_date")
    return conflicts, checks


def _maturity_consistency_flags(bond: SmartLabBond) -> list[str]:
    maturity = bond.fields.get("maturity_date")
    offer = bond.fields.get("offer_date")
    if isinstance(offer, date) and isinstance(maturity, date) and offer > maturity:
        return ["offer_date_after_final_maturity"]
    return []


def _criterion_details(
    bond: SmartLabBond,
    policy: SelectionPolicy,
) -> dict[str, object]:
    """Expose the five first-pass criteria without implying official verification."""
    fields = bond.to_dict()["fields"]
    grade = _canonical_preliminary_rating(bond.fields.get("rating_preliminary"))
    minimum = _canonical_preliminary_rating(policy.minimum_rating)
    if grade is None:
        rating_decision = "NEEDS_CLARIFICATION"
    elif minimum is not None and _RATING_ORDER.index(grade) > _RATING_ORDER.index(minimum):
        rating_decision = "EXCLUDED_BELOW_PRELIMINARY_FLOOR"
    else:
        rating_decision = "PRELIMINARILY_ELIGIBLE"

    rating_group = (
        "missing_or_unrecognized" if grade is None else
        "A_minus_or_higher_priority" if _RATING_ORDER.index(grade) <= _RATING_ORDER.index("A-") else
        "BBB_or_BBB_plus" if grade in {"BBB", "BBB+"} else
        "below_BBB"
    )
    coupon_conflicts, coupon_checks = _coupon_consistency_flags(bond)
    maturity_conflicts = _maturity_consistency_flags(bond)

    years = _remaining_years(bond)
    if years is None:
        maturity_preference = "UNKNOWN"
    elif years <= 0:
        maturity_preference = "PAST_OR_AT_MATURITY"
    elif years <= policy.preferred_maturity_years:
        maturity_preference = "WITHIN_PREFERRED_HORIZON"
    else:
        maturity_preference = "OVER_PREFERRED_HORIZON_NO_AUTOMATIC_REJECTION"

    yield_values = {
        "published_yield_percent": fields.get("reported_yield_percent"),
        "annual_coupon_yield_percent": fields.get("annual_coupon_yield_percent"),
        "last_coupon_yield_percent": fields.get("last_coupon_yield_percent"),
        "g_spread": fields.get("g_spread_bp"),
    }
    available_yield_metrics = [name for name, value in yield_values.items() if value is not None]
    coupon_values = {
        "coupon_amount_rub": fields.get("coupon_rub"),
        "frequency_per_year": fields.get("coupon_frequency_per_year"),
        "next_coupon_date": fields.get("next_coupon_date"),
        "annual_coupon_yield_percent": fields.get("annual_coupon_yield_percent"),
        "last_coupon_yield_percent": fields.get("last_coupon_yield_percent"),
    }
    coupon_values_available = [name for name, value in coupon_values.items() if value is not None]

    return {
        "credit_rating": {
            "listing_value_status": "AVAILABLE" if grade is not None else "MISSING_OR_UNRECOGNIZED",
            "smartlab_value": fields.get("rating_preliminary"),
            "normalized_grade": grade,
            "screening_group": rating_group,
            "preliminary_decision": rating_decision,
            "official_rating_confirmed": False,
            "official_confirmation_required": True,
        },
        "yield": {
            **yield_values,
            "listing_value_status": "AVAILABLE" if available_yield_metrics else "NO_LISTING_VALUES",
            "available_metrics": available_yield_metrics,
            "comparison_rule": "compare like-named Smart-Lab columns only; do not combine different yield methods",
            "published_yield_methodology_confirmed": False,
            "g_spread_unit_and_benchmark_confirmed": False,
            "additional_displayed_columns": dict(bond.other_fields),
        },
        "maturity": {
            "listing_value_status": "PARTIAL" if any(
                fields.get(name) is not None
                for name in ("maturity_date", "years_to_maturity", "offer_date", "duration_years")
            ) else "NO_LISTING_VALUES",
            "final_maturity_date": fields.get("maturity_date"),
            "years_to_final_maturity": fields.get("years_to_maturity"),
            "offer_date": fields.get("offer_date"),
            "duration_years": fields.get("duration_years"),
            "preferred_horizon_status": maturity_preference,
            "offer_is_guaranteed_maturity": False,
            "duration_used_as_maturity": False,
            "official_schedule_confirmation_required": True,
            "consistency_flags": maturity_conflicts,
        },
        "liquidity": {
            "listing_value_status": "VOLUME_AVAILABLE_PERIOD_UNKNOWN" if fields.get("volume_mln_rub") is not None else "NO_LISTING_VOLUME",
            "reported_volume_mln_rub": fields.get("volume_mln_rub"),
            "volume_period": None,
            "volume_calculation": None,
            "is_confirmed_liquidity_measure": False,
            "bid_price": None,
            "ask_price": None,
            "bid_ask_spread": None,
            "order_book_depth": None,
            "trade_frequency": None,
            "missing_market_depth_requires_follow_up": True,
        },
        "coupon": {
            **coupon_values,
            "listing_value_status": "PARTIAL" if coupon_values_available else "NO_LISTING_VALUES",
            "coupon_type": None,
            "future_coupon_formula": None,
            "future_terms_status": "REQUIRES_ISSUE_DOCUMENT_CHECK",
            "terms_verification_required": True,
            "consistency_flags": coupon_conflicts,
            "additional_checks": coupon_checks,
        },
    }


def _criterion_completeness(bonds: list[SmartLabBond]) -> dict[str, object]:
    total = len(bonds)
    field_names = {
        "credit_rating": ("rating_preliminary",),
        "yield": (
            "reported_yield_percent", "annual_coupon_yield_percent",
            "last_coupon_yield_percent", "g_spread_bp",
        ),
        "maturity": ("maturity_date", "years_to_maturity", "offer_date", "duration_years"),
        "liquidity": ("volume_mln_rub",),
        "coupon": (
            "coupon_rub", "coupon_frequency_per_year", "next_coupon_date",
            "annual_coupon_yield_percent", "last_coupon_yield_percent",
        ),
    }
    result: dict[str, object] = {}
    for criterion, names in field_names.items():
        available = {
            name: sum(bond.fields.get(name) is not None for bond in bonds)
            for name in names
        }
        result[criterion] = {
            "records_with_any_listing_value": sum(
                any(bond.fields.get(name) is not None for name in names) for bond in bonds
            ),
            "records_without_listing_values": sum(
                not any(bond.fields.get(name) is not None for name in names) for bond in bonds
            ),
            "field_counts": available,
            "total_records": total,
        }
    result["credit_rating"].update({
        "official_confirmation_required_for_all": total,
        "priority_A_minus_or_higher": sum(
            (grade := _canonical_preliminary_rating(bond.fields.get("rating_preliminary"))) is not None
            and _RATING_ORDER.index(grade) <= _RATING_ORDER.index("A-")
            for bond in bonds
        ),
    })
    result["liquidity"].update({
        "volume_period_known": 0,
        "bid_ask_spread_available_from_listing": 0,
        "order_book_depth_available_from_listing": 0,
        "trade_frequency_available_from_listing": 0,
    })
    result["maturity"].update({
        "records_with_detected_field_conflicts": sum(
            bool(_maturity_consistency_flags(bond)) for bond in bonds
        ),
    })
    result["coupon"].update({
        "future_terms_require_document_check": total,
        "records_with_detected_coupon_field_conflicts": sum(
            bool(_coupon_consistency_flags(bond)[0])
            for bond in bonds
        ),
    })
    return result


def make_search_report(result: SmartLabSearchResult, policy: SelectionPolicy) -> dict[str, object]:
    """Attach the current policy interpretation without claiming due diligence."""
    candidates: list[dict[str, object]] = []
    counts = {"ПРЕДВАРИТЕЛЬНО ПОДХОДИТ": 0, "НЕ ПОДХОДИТ": 0, "ТРЕБУЕТ УТОЧНЕНИЯ": 0}
    rating_groups = {
        "A_and_higher_including_A_minus": 0,
        "BBB_and_BBB_plus": 0,
        "below_BBB_preliminary": 0,
        "missing_or_unrecognized": 0,
    }
    rating_status_counts = {
        group: {status: 0 for status in counts}
        for group in rating_groups
    }
    maturity_groups = {
        "within_preferred_three_years": 0,
        "over_three_years_preference_deviation": 0,
        "past_or_at_maturity": 0,
        "unknown": 0,
    }
    for bond in result.bonds:
        assessment = assess_preliminary(bond, policy)
        row = bond.to_dict()
        row["screening"] = assessment.to_dict()
        row["criteria"] = _criterion_details(bond, policy)
        candidates.append(row)
        counts[assessment.status] += 1
        grade = _canonical_preliminary_rating(bond.fields.get("rating_preliminary"))
        if grade is None:
            rating_group = "missing_or_unrecognized"
        elif _RATING_ORDER.index(grade) <= _RATING_ORDER.index("A-"):
            rating_group = "A_and_higher_including_A_minus"
        elif grade in {"BBB", "BBB+"}:
            rating_group = "BBB_and_BBB_plus"
        else:
            rating_group = "below_BBB_preliminary"
        rating_groups[rating_group] += 1
        rating_status_counts[rating_group][assessment.status] += 1
        years = _remaining_years(bond)
        if years is None:
            maturity_groups["unknown"] += 1
        elif years <= 0:
            maturity_groups["past_or_at_maturity"] += 1
        elif years <= policy.preferred_maturity_years:
            maturity_groups["within_preferred_three_years"] += 1
        else:
            maturity_groups["over_three_years_preference_deviation"] += 1
    return {
        "status": "PRELIMINARY_SCREEN_COMPLETE" if result.status in {"COMPLETE", "LIMIT_REACHED"} else result.status,
        "source": "Smart-Lab",
        "source_url": SMARTLAB_LISTING_URL,
        "retrieved_at": result.retrieved_at.isoformat(),
        "listing_pages_fetched": result.pages_fetched,
        "preliminary_summary": {
            "candidate_count": len(result.bonds),
            "status_counts": counts,
            "rating_groups": rating_groups,
            "rating_status_counts": rating_status_counts,
            "maturity_groups": maturity_groups,
            "criterion_completeness": _criterion_completeness(list(result.bonds)),
        },
        "field_interpretation": {
            "rating_preliminary": "Smart-Lab rating is only a screening signal; agency, scale, issue subject and current action require confirmation.",
            "instrument_type": "A bond-listing row is not official confirmation that the issue is corporate; explicit OFZ, government or municipal labels are excluded.",
            "reported_yield_percent": "Keep separate from annual and last-coupon yield; no fixed minimum is applied, and methodology/reference price require confirmation.",
            "annual_coupon_yield_percent": "A distinct Smart-Lab column; do not merge with published yield or last-coupon yield.",
            "last_coupon_yield_percent": "A distinct Smart-Lab column; do not treat one current coupon as proof of fixed future terms.",
            "g_spread_bp": "Preserved as displayed; the listing header does not establish unit, benchmark, tenor or calculation method.",
            "volume_mln_rub": "Reported volume is shown in million rubles, but its period/calculation is not established and it is not a bid-ask, depth or direct liquidity measure.",
            "duration_years": "Duration is a separate risk measure; maturity preference uses only maturity date or years-to-maturity.",
            "offer_date": "An offer date is a separate event and does not guarantee repayment at that date.",
            "coupon_terms": "Coupon type and future formula cannot be verified from the listing; inspect current issue documents before treating future payments as known.",
        },
        "source_diagnostics": result.source_diagnostics or {},
        "pagination": {
            "status": result.status,
            "collection_mode": "ALL_AVAILABLE_PAGES" if result.collect_all else "LIMITED_SAMPLE",
            "full_scan_requested": result.collect_all,
            "full_scan_complete": result.collect_all and result.status == "COMPLETE",
            "requested_unique_limit": result.requested_max_items,
            "maximum_unique_limit": None if result.collect_all else _MAX_UNIQUE_ITEMS,
            "rows_per_observed_site_page": _PAGE_SIZE,
            "maximum_pages_per_call": _MAX_PAGE_REQUESTS,
            "pages_fetched": result.pages_fetched,
            "unique_isins": len(result.bonds),
            "duplicate_rows": result.duplicate_rows,
            "rows_without_isin": result.rows_without_isin,
            "row_sequence_verified": result.row_sequence_verified,
            "sort_signature": result.sort_signature,
            "integrity_issues": list(result.integrity_issues),
            "schema_warnings": list(result.schema_warnings),
            "source_error": result.source_error,
        },
        "policy": {
            "fingerprint": policy.snapshot.fingerprint,
            "fetched_at": policy.snapshot.fetched_at.isoformat(),
            "rules_loaded": len(policy.snapshot.rules),
            "registered_sources_loaded": len(policy.snapshot.resources),
            "minimum_rating": policy.minimum_rating,
            "preferred_maturity_years": policy.preferred_maturity_years,
            "bbb_units_limit": policy.bbb_units_limit,
            "bbb_plus_cap_is_working_interpretation": policy.bbb_plus_is_working_interpretation,
            "minimum_yield_percent": None,
            "nominal_constraint": None,
            "current_portfolio_counts_loaded": False,
        },
        "result_counts": counts,
        "candidates": candidates,
        "limitations": [
            "Smart-Lab rating is preliminary and requires confirmation by a current rating agency.",
            "The listing does not establish issuer country or payment currency; both require verification.",
            "BBB/BBB+ issuer concentration cannot be claimed compliant because current positions across accounts were not read.",
            "The screen does not disclose coupon type/future formula, confirm issue terms, or establish executable market liquidity.",
            "The listing volume period and calculation, order-book depth, bid/ask spread and trade frequency are not available from this table.",
            "Yield fields are distinct published columns; their methods, benchmark alignment and reference prices are not fully specified here.",
            "No individual bond detail page was requested; no portfolio data was written.",
            "A missing listing value is returned as null and listed under needs_verification.",
        ],
    }
