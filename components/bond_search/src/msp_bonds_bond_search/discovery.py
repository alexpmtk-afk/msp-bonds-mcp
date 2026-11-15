"""Bounded read-only discovery and an independent MOEX ISS identifier check."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html.parser import HTMLParser
import http.client
import json
import re
import socket
import ssl
import threading
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

_ISIN = re.compile(r"(?<![A-Z0-9])RU[A-Z0-9]{10}(?![A-Z0-9])")
_MAX_BYTES = 3_000_000
_MAX_TIMEOUT_SECONDS = 10.0
_ALLOWED_HOSTS = frozenset({"smart-lab.ru", "iss.moex.com"})
_USER_AGENT = "MSPBonds-Research/0.1"
_HOST_LOCK = threading.Lock()
_LAST_HOST_REQUEST: dict[str, float] = {}
_MIN_HOST_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class FetchDiagnostic:
    host: str
    phase: str
    attempt: int
    elapsed_seconds: float
    response_bytes: int = 0
    http_status: int | None = None
    reason_code: str = ""


class SourceUnavailable(RuntimeError):
    """A source cannot provide the required evidence; use another source."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "SOURCE_UNAVAILABLE",
        retryable: bool = False,
        http_status: int | None = None,
        phase: str = "",
        attempts: int = 1,
        response_bytes: int = 0,
        elapsed_seconds: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status
        self.phase = phase
        self.attempts = attempts
        self.response_bytes = response_bytes
        self.elapsed_seconds = elapsed_seconds

    @property
    def diagnostic_summary(self) -> str:
        parts = [self.code]
        if self.phase:
            parts.append(f"phase={self.phase}")
        parts.append(f"attempts={self.attempts}")
        if self.http_status is not None:
            parts.append(f"http_status={self.http_status}")
        parts.append(f"response_bytes={self.response_bytes}")
        return " ".join(parts)


@dataclass(frozen=True)
class Candidate:
    isin: str
    name: str
    discovered_via: str
    source_url: str
    fetched_at: datetime
    status: str = "DISCOVERED_UNVERIFIED"
    independent_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _ISIN.fullmatch(self.isin):
            raise ValueError("candidate must have a Russian-format ISIN")
        if self.fetched_at.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        parts = urlsplit(self.source_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("candidate source must be a safe HTTPS URL")


@dataclass(frozen=True)
class MoexIsinCheck:
    isin: str
    matched: bool
    source_url: str
    fetched_at: datetime
    security_id: str = ""
    short_name: str = ""
    state: str = "UNVERIFIED"
    attempts: int = 1
    response_bytes: int = 0
    elapsed_seconds: float = 0.0


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise SourceUnavailable("redirect blocked; source endpoint must be verified", code="REDIRECT_BLOCKED", phase="http")


def _rate_limit(host: str) -> None:
    """Serialize requests per host and leave at least one second between them."""
    with _HOST_LOCK:
        now = time.monotonic()
        previous = _LAST_HOST_REQUEST.get(host)
        delay = 0.0 if previous is None else _MIN_HOST_INTERVAL_SECONDS - (now - previous)
        if delay > 0:
            time.sleep(delay)
        _LAST_HOST_REQUEST[host] = time.monotonic()


def _notify(observer: Callable[[FetchDiagnostic], None] | None, item: FetchDiagnostic) -> None:
    if observer is None:
        return
    try:
        observer(item)
    except Exception:
        # Diagnostics must never change request behavior.
        return


def _error_reason(exc: BaseException, phase: str) -> tuple[str, bool]:
    """Map transport errors to a bounded, non-sensitive diagnostic code."""
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, socket.gaierror):
        return "DNS_FAILURE", True
    if isinstance(reason, ssl.SSLError):
        return "TLS_FAILURE", False
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return ("READ_TIMEOUT" if phase == "read" else "CONNECT_TIMEOUT"), True
    if isinstance(reason, (ConnectionResetError, ConnectionAbortedError, ConnectionRefusedError)):
        return "CONNECTION_FAILURE", True
    if isinstance(reason, http.client.IncompleteRead):
        return "INCOMPLETE_RESPONSE", True
    return "NETWORK_FAILURE", False


def _request_once(
    url: str,
    *,
    timeout: float,
    attempt: int,
    observer: Callable[[FetchDiagnostic], None] | None = None,
) -> bytes:
    started = time.monotonic()
    try:
        parts = urlsplit(url)
        port = parts.port
    except (TypeError, ValueError) as exc:
        raise SourceUnavailable("invalid outbound address", code="INVALID_URL", phase="validate") from exc
    if (
        parts.scheme != "https"
        or parts.hostname not in _ALLOWED_HOSTS
        or parts.username or parts.password or port
        or parts.fragment
    ):
        raise SourceUnavailable("non-allowlisted outbound address", code="URL_NOT_ALLOWED", phase="validate")
    _rate_limit(parts.hostname)
    request = Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.5",
        },
    )
    phase = "connect"
    try:
        response = build_opener(_NoRedirect()).open(request, timeout=float(timeout))
    except HTTPError as exc:
        status = int(exc.code)
        retryable = 500 <= status <= 504
        elapsed = time.monotonic() - started
        _notify(observer, FetchDiagnostic(
            parts.hostname, "http", attempt, elapsed, 0, status, f"HTTP_{status}",
        ))
        raise SourceUnavailable(
            "source returned an HTTP error",
            code=f"HTTP_{status}", retryable=retryable, http_status=status,
            phase="http", attempts=attempt, elapsed_seconds=elapsed,
        ) from None
    except SourceUnavailable:
        raise
    except (URLError, TimeoutError, OSError, ssl.SSLError) as exc:
        code, retryable = _error_reason(exc, phase)
        elapsed = time.monotonic() - started
        _notify(observer, FetchDiagnostic(
            parts.hostname, phase, attempt, elapsed, 0, None, code,
        ))
        raise SourceUnavailable(
            "source connection failed",
            code=code, retryable=retryable, phase=phase,
            attempts=attempt, elapsed_seconds=elapsed,
        ) from None

    with response:
        try:
            response_host = urlsplit(response.geturl()).hostname
        except ValueError:
            raise SourceUnavailable("invalid response address", code="INVALID_RESPONSE_URL", phase="http") from None
        if response_host != parts.hostname:
            raise SourceUnavailable("unexpected response host", code="RESPONSE_HOST_MISMATCH", phase="http")
        phase = "read"
        try:
            raw = response.read(_MAX_BYTES + 1)
        except (http.client.IncompleteRead, TimeoutError, OSError, ssl.SSLError) as exc:
            code, retryable = _error_reason(exc, phase)
            elapsed = time.monotonic() - started
            _notify(observer, FetchDiagnostic(
                parts.hostname, phase, attempt, elapsed, 0, getattr(response, "status", None), code,
            ))
            raise SourceUnavailable(
                "source response could not be read",
                code=code, retryable=retryable, http_status=getattr(response, "status", None),
                phase=phase, attempts=attempt, elapsed_seconds=elapsed,
            ) from None
        status = getattr(response, "status", 200)
    elapsed = time.monotonic() - started
    _notify(observer, FetchDiagnostic(
        parts.hostname, "complete", attempt, elapsed, len(raw), status, "",
    ))
    if len(raw) > _MAX_BYTES:
        raise SourceUnavailable(
            "oversized source response", code="RESPONSE_TOO_LARGE",
            phase="read", attempts=attempt, response_bytes=len(raw), elapsed_seconds=elapsed,
        )
    return raw


def safe_get(
    url: str,
    timeout: float = _MAX_TIMEOUT_SECONDS,
    *,
    max_attempts: int = 1,
    observer: Callable[[FetchDiagnostic], None] | None = None,
) -> bytes:
    """Bounded HTTPS GET; retries only transient DNS/network/timeouts and MOEX 5xx."""
    if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > _MAX_TIMEOUT_SECONDS:
        raise SourceUnavailable("request timeout must be between 0 and 10 seconds", code="INVALID_TIMEOUT")
    if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 3:
        raise SourceUnavailable("request attempts must be 1..3", code="INVALID_ATTEMPTS")
    last_error: SourceUnavailable | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _request_once(url, timeout=float(timeout), attempt=attempt, observer=observer)
        except SourceUnavailable as exc:
            last_error = exc
            if not exc.retryable or attempt >= max_attempts:
                if exc.attempts == attempt:
                    raise
                raise SourceUnavailable(
                    str(exc), code=exc.code, retryable=exc.retryable,
                    http_status=exc.http_status, phase=exc.phase, attempts=attempt,
                    response_bytes=exc.response_bytes, elapsed_seconds=exc.elapsed_seconds,
                ) from None
            time.sleep(min(0.25 * attempt, 0.5))
    assert last_error is not None
    raise last_error


def _fetch_moex(
    url: str,
    fetch: Callable[[str], bytes],
    *,
    observer: Callable[[FetchDiagnostic], None] | None = None,
) -> tuple[bytes, int, float]:
    started = time.monotonic()
    events: list[FetchDiagnostic] = []

    def record(item: FetchDiagnostic) -> None:
        events.append(item)
        _notify(observer, item)

    if fetch is safe_get:
        payload = safe_get(url, max_attempts=2, observer=record)
        attempts = max((item.attempt for item in events), default=1)
        elapsed = sum(item.elapsed_seconds for item in events)
        return payload, attempts, elapsed

    try:
        payload = fetch(url)
    except SourceUnavailable:
        raise
    except Exception as exc:
        raise SourceUnavailable(
            "injected source reader failed",
            code="ADAPTER_FAILURE", phase="request",
            elapsed_seconds=time.monotonic() - started,
        ) from None
    if not isinstance(payload, (bytes, bytearray)):
        raise SourceUnavailable("source reader returned a non-byte response", code="INVALID_RESPONSE")
    payload = bytes(payload)
    _notify(observer, FetchDiagnostic(
        "iss.moex.com", "complete", 1, time.monotonic() - started, len(payload), 200, "",
    ))
    return payload, 1, time.monotonic() - started


class _Rows(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._table_depth = 0
        self._in_row = False
        self._in_cell = False
        self._cell_text: list[str] = []
        self._cells: list[str] = []
        self._links: list[str] = []
        self.rows: list[tuple[list[str], list[str]]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self._table_depth += 1
        elif self._table_depth and tag == "tr":
            self._in_row = True
            self._cells, self._links = [], []
        elif self._in_row and tag in ("td", "th"):
            self._in_cell = True
            self._cell_text = []
        elif self._in_cell and tag == "a":
            href = dict(attrs).get("href")
            if isinstance(href, str):
                self._links.append(href)

    def handle_data(self, text: str) -> None:
        if self._in_cell:
            self._cell_text.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_cell:
            self._cells.append(" ".join(" ".join(self._cell_text).split()))
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            self.rows.append((self._cells[:], self._links[:]))
            self._in_row = False
            self._in_cell = False
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1


def parse_smartlab(html: str, *, fetched_at: datetime, max_items: int = 100) -> list[Candidate]:
    if not 1 <= max_items <= 100:
        raise ValueError("max_items must be 1..100")
    parser = _Rows()
    parser.feed(html)
    unique: dict[str, Candidate] = {}
    for cells, links in parser.rows:
        match = _ISIN.search(" ".join(links + cells))
        if not match or not cells:
            continue
        isin = match.group()
        name = cells[1] if len(cells) > 1 else cells[0]
        if not name or isin in unique:
            continue
        unique[isin] = Candidate(
            isin=isin, name=name, discovered_via="Smart-Lab",
            source_url=f"https://smart-lab.ru/q/bonds/{isin}/", fetched_at=fetched_at,
        )
        if len(unique) >= max_items:
            break
    return list(unique.values())


def smartlab_robots_allow(fetch: Callable[[str], bytes] = safe_get) -> bool:
    """Fail closed if the site's robots policy cannot be read or forbids the path."""
    url = "https://smart-lab.ru/robots.txt"
    raw = fetch(url)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceUnavailable("Smart-Lab robots file encoding") from exc
    if not text.strip():
        raise SourceUnavailable("Smart-Lab robots policy is empty or unavailable")
    parser = RobotFileParser(url)
    parser.parse(text.splitlines())
    return parser.can_fetch(_USER_AGENT, "https://smart-lab.ru/q/bonds/")


def discover_smartlab(
    *,
    fetch: Callable[[str], bytes] = safe_get,
    now: datetime | None = None,
    max_items: int = 100,
) -> list[Candidate]:
    if not smartlab_robots_allow(fetch):
        raise SourceUnavailable("Smart-Lab robots policy disallows the discovery path")
    raw = fetch("https://smart-lab.ru/q/bonds/")
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceUnavailable("Smart-Lab response encoding") from exc
    candidates = parse_smartlab(
        html, fetched_at=now or datetime.now(timezone.utc), max_items=max_items,
    )
    if not candidates:
        raise SourceUnavailable("Smart-Lab page lacks ISIN-tagged rows")
    return candidates


def _moex_table(payload: bytes) -> tuple[list[str], list[list[object]]]:
    try:
        root = json.loads(payload)
        table = root["securities"]
        columns = table["columns"]
        rows = table["data"]
        if not isinstance(columns, list) or not isinstance(rows, list):
            raise ValueError("invalid MOEX ISS table")
        return [str(value) for value in columns], rows
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceUnavailable("invalid MOEX ISS response", code="PARSE_ERROR", phase="parse") from None


def parse_moex(payload: bytes, *, fetched_at: datetime, max_items: int = 100) -> list[Candidate]:
    columns, rows = _moex_table(payload)
    unique: dict[str, Candidate] = {}
    for row in rows:
        if not isinstance(row, list):
            continue
        data = dict(zip(columns, row))
        isin = str(data.get("ISIN") or data.get("SECID") or "").strip().upper()
        if not _ISIN.fullmatch(isin):
            continue
        name = str(data.get("SHORTNAME") or data.get("SECNAME") or data.get("NAME") or isin)
        unique.setdefault(
            isin,
            Candidate(
                isin=isin, name=name, discovered_via="MOEX ISS",
                source_url=f"https://iss.moex.com/iss/securities.json?q={isin}",
                fetched_at=fetched_at,
            ),
        )
        if len(unique) >= max_items:
            break
    return list(unique.values())


def discover_moex(
    *,
    fetch: Callable[[str], bytes] = safe_get,
    now: datetime | None = None,
    max_items: int = 100,
) -> list[Candidate]:
    if not isinstance(max_items, int) or not 1 <= max_items <= 100:
        raise ValueError("max_items must be 1..100")
    params = urlencode({
        "iss.meta": "off", "iss.only": "securities",
        "start": 0, "limit": max_items,
    })
    url = "https://iss.moex.com/iss/engines/stock/markets/bonds/securities.json?" + params
    payload, _, _ = _fetch_moex(url, fetch)
    result = parse_moex(payload, fetched_at=now or datetime.now(timezone.utc), max_items=max_items)
    if not result:
        raise SourceUnavailable("MOEX ISS returned no ISIN-tagged rows in the requested page")
    return result


def lookup_moex_isin(
    isin: str,
    *,
    fetch: Callable[[str], bytes] = safe_get,
    now: datetime | None = None,
    observer: Callable[[FetchDiagnostic], None] | None = None,
) -> MoexIsinCheck:
    """Check one Smart-Lab lead against the official MOEX ISS securities index."""
    if not _ISIN.fullmatch(isin):
        raise ValueError("a Russian-format ISIN is required")
    params = urlencode({
        "q": isin, "limit": 10, "iss.meta": "off", "iss.only": "securities",
    })
    url = "https://iss.moex.com/iss/securities.json?" + params
    started = time.monotonic()
    payload, attempts, request_elapsed = _fetch_moex(url, fetch, observer=observer)
    columns, rows = _moex_table(payload)
    match: dict[str, object] | None = None
    for row in rows:
        if not isinstance(row, list):
            continue
        data = dict(zip(columns, row))
        if str(data.get("ISIN") or "").strip().upper() == isin:
            match = data
            break

    response_bytes = len(payload)
    checked_url = url
    # The documented Security Finder may return no hit for an ISIN even when
    # the bond is in the official bonds list. Confirm against that list before
    # reporting an exact miss; never turn the finder miss into a negative fact.
    if match is None:
        listing_url = (
            "https://iss.moex.com/iss/engines/stock/markets/bonds/securities.json?"
            + urlencode({"iss.meta": "off", "iss.only": "securities", "start": 0})
        )
        listing_payload, listing_attempts, listing_elapsed = _fetch_moex(
            listing_url, fetch, observer=observer,
        )
        listing_columns, listing_rows = _moex_table(listing_payload)
        for row in listing_rows:
            if not isinstance(row, list):
                continue
            data = dict(zip(listing_columns, row))
            if str(data.get("ISIN") or "").strip().upper() == isin:
                match = data
                break
        payload = listing_payload
        checked_url = listing_url
        response_bytes += len(listing_payload)
        attempts += listing_attempts
        request_elapsed += listing_elapsed

    timestamp = now or datetime.now(timezone.utc)
    return MoexIsinCheck(
        isin=isin,
        matched=match is not None,
        source_url=checked_url,
        fetched_at=timestamp,
        security_id=str((match or {}).get("SECID") or ""),
        short_name=str((match or {}).get("SHORTNAME") or (match or {}).get("SECNAME") or ""),
        state=(
            "IDENTIFIER_CONFIRMED_ON_MOEX"
            if match is not None else "NO_EXACT_MATCH_IN_CURRENT_BOND_LIST"
        ),
        attempts=attempts,
        response_bytes=response_bytes,
        elapsed_seconds=max(request_elapsed, time.monotonic() - started),
    )
