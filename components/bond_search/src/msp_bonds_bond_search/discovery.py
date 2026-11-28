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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

_ISIN = re.compile(r"(?<![A-Z0-9])RU[A-Z0-9]{10}(?![A-Z0-9])")
_MAX_BYTES = 3_000_000
_MAX_TIMEOUT_SECONDS = 10.0
_SMARTLAB_READ_TIMEOUT_SECONDS = 30.0
_SMARTLAB_RESPONSE_DEADLINE_SECONDS = 60.0
_READ_CHUNK_BYTES = 64 * 1024
_ALLOWED_HOSTS = frozenset({"smart-lab.ru", "iss.moex.com"})
_USER_AGENT = "MSPBonds-Research/0.1"
_HOST_LOCK = threading.Lock()
_LAST_HOST_REQUEST: dict[str, float] = {}
_MIN_HOST_INTERVAL_SECONDS = 1.0
_MOEX_PAGE_LIMIT = 1000
_MOEX_MAX_PAGE_COUNT = 20
_MOEX_SCAN_BUDGET_SECONDS = 45.0
_MOEX_REQUEST_TIMEOUT_SECONDS = 10.0


def _diagnostic_url(url: str) -> str:
    """Return the allowlisted request URL without credentials or secret query values."""
    try:
        parts = urlsplit(url)
    except (TypeError, ValueError):
        return ""
    if (
        parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS
        or parts.username or parts.password
    ):
        return ""
    safe_query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not re.search(r"token|secret|password|credential|auth|api.?key|signature", key, re.I)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), ""))


@dataclass(frozen=True)
class FetchDiagnostic:
    host: str
    phase: str
    attempt: int
    elapsed_seconds: float
    response_bytes: int = 0
    http_status: int | None = None
    reason_code: str = ""
    url: str = ""
    content_length: int | None = None
    transfer_encoding: str = ""
    content_encoding: str = ""
    body_framing: str = ""
    body_complete: bool | None = None
    wire_bytes: int | None = None
    curl_exit_code: int | None = None
    cache_status: str = ""
    cache_age_seconds: float | None = None
    content_fetched_at: str = ""


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
        source_url: str = "",
        request_diagnostics: tuple[dict[str, object], ...] = (),
        diagnostic_context: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status
        self.phase = phase
        self.attempts = attempts
        self.response_bytes = response_bytes
        self.elapsed_seconds = elapsed_seconds
        self.source_url = _diagnostic_url(source_url) if source_url else ""
        self.request_diagnostics = request_diagnostics
        self.diagnostic_context = diagnostic_context

    @property
    def diagnostic_summary(self) -> str:
        parts = [self.code]
        if self.phase:
            parts.append(f"phase={self.phase}")
        parts.append(f"attempts={self.attempts}")
        if self.http_status is not None:
            parts.append(f"http_status={self.http_status}")
        parts.append(f"response_bytes={self.response_bytes}")
        if self.source_url:
            parts.append(f"url={self.source_url}")
        if self.diagnostic_context:
            parts.append(f"trace={self.diagnostic_context}")
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
    scanned_pages: int = 0
    scanned_rows: int = 0
    scan_complete: bool = False
    scan_range: str = ""


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


def _response_header(response, name: str) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    try:
        value = headers.get(name, "")
    except (AttributeError, TypeError):
        return ""
    return value.strip() if isinstance(value, str) else ""


def _safe_header_token(value: str) -> str:
    """Keep only harmless token characters from server-supplied diagnostic headers."""
    return re.sub(r"[^a-zA-Z0-9,._-]", "", value.strip().lower())[:80]


def _parse_content_length(value: str) -> int | None:
    if not value:
        return None
    parts = [part.strip() for part in value.split(",")]
    if not parts or any(not part.isdigit() for part in parts) or len(set(parts)) != 1:
        raise SourceUnavailable(
            "source returned an invalid Content-Length",
            code="INVALID_CONTENT_LENGTH", phase="read",
        )
    try:
        return int(parts[0])
    except ValueError:
        raise SourceUnavailable(
            "source returned an invalid Content-Length",
            code="INVALID_CONTENT_LENGTH", phase="read",
        ) from None


def _set_response_read_timeout(response, timeout: float) -> bool:
    """Set the socket read timeout after urllib has completed the response headers."""
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    setter = getattr(sock, "settimeout", None)
    if not callable(setter):
        return False
    setter(float(timeout))
    return True


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
    read_timeout: float | None = None,
    observer: Callable[[FetchDiagnostic], None] | None = None,
) -> bytes:
    started = time.monotonic()
    diagnostic_url = _diagnostic_url(url)
    try:
        parts = urlsplit(url)
        port = parts.port
    except (TypeError, ValueError) as exc:
        raise SourceUnavailable(
            "invalid outbound address", code="INVALID_URL", phase="validate",
            source_url=diagnostic_url,
        ) from exc
    if (
        parts.scheme != "https"
        or parts.hostname not in _ALLOWED_HOSTS
        or parts.username or parts.password or port
        or parts.fragment
    ):
        raise SourceUnavailable(
            "non-allowlisted outbound address", code="URL_NOT_ALLOWED",
            phase="validate", source_url=diagnostic_url,
        )
    _rate_limit(parts.hostname)
    request = Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.5",
            "Accept-Encoding": "identity",
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
            diagnostic_url,
        ))
        raise SourceUnavailable(
            "source returned an HTTP error",
            code=f"HTTP_{status}", retryable=retryable, http_status=status,
            phase="http", attempts=attempt, elapsed_seconds=elapsed,
            source_url=diagnostic_url,
        ) from None
    except SourceUnavailable as exc:
        if not exc.source_url:
            exc.source_url = diagnostic_url
        raise
    except (URLError, TimeoutError, OSError, ssl.SSLError) as exc:
        code, retryable = _error_reason(exc, phase)
        elapsed = time.monotonic() - started
        _notify(observer, FetchDiagnostic(
            parts.hostname, phase, attempt, elapsed, 0, None, code,
            diagnostic_url,
        ))
        raise SourceUnavailable(
            "source connection failed",
            code=code, retryable=retryable, phase=phase,
            attempts=attempt, elapsed_seconds=elapsed,
            source_url=diagnostic_url,
        ) from None

    with response:
        try:
            response_host = urlsplit(response.geturl()).hostname
        except ValueError:
            raise SourceUnavailable(
                "invalid response address", code="INVALID_RESPONSE_URL",
                phase="http", source_url=diagnostic_url,
            ) from None
        if response_host != parts.hostname:
            raise SourceUnavailable(
                "unexpected response host", code="RESPONSE_HOST_MISMATCH",
                phase="http", source_url=diagnostic_url,
            )
        phase = "read"
        content_length_text = _response_header(response, "Content-Length")
        transfer_encoding = _safe_header_token(_response_header(response, "Transfer-Encoding"))
        content_encoding = _safe_header_token(_response_header(response, "Content-Encoding"))
        response_status = getattr(response, "status", None)
        try:
            content_length = _parse_content_length(content_length_text)
        except SourceUnavailable as exc:
            _notify(observer, FetchDiagnostic(
                parts.hostname, phase, attempt, time.monotonic() - started,
                http_status=response_status, reason_code=exc.code,
                url=diagnostic_url, transfer_encoding=transfer_encoding,
                content_encoding=content_encoding, body_framing="invalid",
                body_complete=False,
            ))
            raise SourceUnavailable(
                "source response has invalid framing headers",
                code=exc.code, phase=phase, attempts=attempt,
                http_status=response_status, source_url=diagnostic_url,
            ) from None

        is_chunked = transfer_encoding == "chunked"
        body_framing = "chunked" if is_chunked else (
            "content-length" if content_length is not None else "connection-close"
        )

        def notify_read_failure(code: str, response_bytes: int) -> None:
            _notify(observer, FetchDiagnostic(
                parts.hostname, phase, attempt, time.monotonic() - started,
                response_bytes, response_status, code, diagnostic_url,
                content_length, transfer_encoding, content_encoding,
                body_framing, False,
            ))

        if transfer_encoding and not is_chunked:
            notify_read_failure("UNSUPPORTED_TRANSFER_ENCODING", 0)
            raise SourceUnavailable(
                "source used an unsupported transfer encoding",
                code="UNSUPPORTED_TRANSFER_ENCODING", phase=phase,
                http_status=response_status, source_url=diagnostic_url,
            )
        if is_chunked and content_length is not None:
            notify_read_failure("AMBIGUOUS_RESPONSE_FRAMING", 0)
            raise SourceUnavailable(
                "source returned both chunked transfer encoding and Content-Length",
                code="AMBIGUOUS_RESPONSE_FRAMING", phase=phase,
                http_status=response_status, source_url=diagnostic_url,
            )
        if content_encoding not in {"", "identity"}:
            notify_read_failure("UNSUPPORTED_CONTENT_ENCODING", 0)
            raise SourceUnavailable(
                "source returned an unsupported content encoding",
                code="UNSUPPORTED_CONTENT_ENCODING", phase=phase,
                http_status=response_status, source_url=diagnostic_url,
            )
        if content_length is not None and content_length > _MAX_BYTES:
            notify_read_failure("RESPONSE_TOO_LARGE", 0)
            raise SourceUnavailable(
                "oversized source response declared by Content-Length",
                code="RESPONSE_TOO_LARGE", phase=phase,
                http_status=response_status, response_bytes=0,
                source_url=diagnostic_url,
            )
        if read_timeout is not None and not _set_response_read_timeout(response, read_timeout):
            notify_read_failure("READ_TIMEOUT_CONFIGURATION_FAILED", 0)
            raise SourceUnavailable(
                "bounded response read timeout could not be configured",
                code="READ_TIMEOUT_CONFIGURATION_FAILED", phase=phase,
                http_status=response_status, source_url=diagnostic_url,
            )

        raw_buffer = bytearray()
        read_deadline = (
            time.monotonic() + _SMARTLAB_RESPONSE_DEADLINE_SECONDS
            if read_timeout is not None else None
        )
        try:
            reader = getattr(response, "read1", None)
            if not callable(reader):
                reader = response.read
            while len(raw_buffer) < _MAX_BYTES + 1:
                if content_length is not None:
                    remaining_body = content_length - len(raw_buffer)
                    if remaining_body == 0:
                        break
                    requested = min(_READ_CHUNK_BYTES, remaining_body)
                else:
                    requested = min(_READ_CHUNK_BYTES, _MAX_BYTES + 1 - len(raw_buffer))
                if read_deadline is not None:
                    remaining_time = read_deadline - time.monotonic()
                    if remaining_time <= 0:
                        raise TimeoutError("bounded response read deadline exceeded")
                    _set_response_read_timeout(response, min(read_timeout, remaining_time))
                chunk = reader(requested)
                if not chunk:
                    if content_length is not None and len(raw_buffer) != content_length:
                        notify_read_failure("INCOMPLETE_RESPONSE", len(raw_buffer))
                        raise SourceUnavailable(
                            "source ended before its declared Content-Length",
                            code="INCOMPLETE_RESPONSE", retryable=True,
                            http_status=response_status, phase=phase,
                            attempts=attempt, response_bytes=len(raw_buffer),
                            elapsed_seconds=time.monotonic() - started,
                            source_url=diagnostic_url,
                        )
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    notify_read_failure("INVALID_RESPONSE", len(raw_buffer))
                    raise SourceUnavailable(
                        "source response reader returned non-byte data",
                        code="INVALID_RESPONSE", phase=phase, source_url=diagnostic_url,
                    )
                if content_length is not None and len(raw_buffer) + len(chunk) > content_length:
                    notify_read_failure("RESPONSE_LENGTH_MISMATCH", len(raw_buffer) + len(chunk))
                    raise SourceUnavailable(
                        "source sent more bytes than its declared Content-Length",
                        code="RESPONSE_LENGTH_MISMATCH", phase=phase,
                        http_status=response_status,
                        response_bytes=len(raw_buffer) + len(chunk),
                        source_url=diagnostic_url,
                    )
                raw_buffer.extend(chunk)
            raw = bytes(raw_buffer)
        except SourceUnavailable:
            raise
        except (http.client.IncompleteRead, TimeoutError, OSError, ssl.SSLError) as exc:
            partial = getattr(exc, "partial", b"")
            if isinstance(partial, (bytes, bytearray)):
                raw_buffer.extend(partial[:max(0, _MAX_BYTES + 1 - len(raw_buffer))])
            code, retryable = _error_reason(exc, phase)
            elapsed = time.monotonic() - started
            _notify(observer, FetchDiagnostic(
                parts.hostname, phase, attempt, elapsed, len(raw_buffer),
                response_status, code, diagnostic_url, content_length,
                transfer_encoding, content_encoding, body_framing, False,
            ))
            raise SourceUnavailable(
                "source response could not be read",
                code=code, retryable=retryable, http_status=response_status,
                phase=phase, attempts=attempt, response_bytes=len(raw_buffer),
                elapsed_seconds=elapsed, source_url=diagnostic_url,
            ) from None
        status = getattr(response, "status", 200)
    elapsed = time.monotonic() - started
    if len(raw) > _MAX_BYTES:
        _notify(observer, FetchDiagnostic(
            parts.hostname, "read", attempt, elapsed, len(raw), status,
            "RESPONSE_TOO_LARGE", diagnostic_url, content_length,
            transfer_encoding, content_encoding, body_framing, False,
        ))
        raise SourceUnavailable(
            "oversized source response", code="RESPONSE_TOO_LARGE",
            phase="read", attempts=attempt, response_bytes=len(raw), elapsed_seconds=elapsed,
            source_url=diagnostic_url,
        )
    _notify(observer, FetchDiagnostic(
        parts.hostname, "complete", attempt, elapsed, len(raw), status, "",
        diagnostic_url, content_length, transfer_encoding, content_encoding,
        body_framing, True,
    ))
    return raw


def safe_get(
    url: str,
    timeout: float = _MAX_TIMEOUT_SECONDS,
    *,
    max_attempts: int = 1,
    read_timeout: float | None = None,
    observer: Callable[[FetchDiagnostic], None] | None = None,
) -> bytes:
    """Bounded HTTPS GET; retries only transient DNS/network/timeouts and MOEX 5xx."""
    if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > _MAX_TIMEOUT_SECONDS:
        raise SourceUnavailable("request timeout must be between 0 and 10 seconds", code="INVALID_TIMEOUT")
    if read_timeout is not None:
        host = urlsplit(url).hostname
        if (
            host != "smart-lab.ru"
            or not isinstance(read_timeout, (int, float))
            or read_timeout <= 0
            or read_timeout > _SMARTLAB_READ_TIMEOUT_SECONDS
        ):
            raise SourceUnavailable(
                "extended read timeout is only allowed for Smart-Lab and must be at most 30 seconds",
                code="INVALID_READ_TIMEOUT",
            )
    if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 3:
        raise SourceUnavailable("request attempts must be 1..3", code="INVALID_ATTEMPTS")
    last_error: SourceUnavailable | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            request_kwargs = {
                "timeout": float(timeout), "attempt": attempt, "observer": observer,
            }
            if read_timeout is not None:
                request_kwargs["read_timeout"] = float(read_timeout)
            return _request_once(url, **request_kwargs)
        except SourceUnavailable as exc:
            last_error = exc
            if not exc.source_url:
                exc.source_url = _diagnostic_url(url)
            if not exc.retryable or attempt >= max_attempts:
                exc.attempts = attempt
                raise
            time.sleep(min(0.25 * attempt, 0.5))
    assert last_error is not None
    raise last_error


def _fetch_smartlab(
    url: str,
    fetch: Callable[[str], bytes] = safe_get,
    *,
    observer: Callable[[FetchDiagnostic], None] | None = None,
) -> bytes:
    """Use the isolated curl transport for default Smart-Lab requests."""
    if fetch is safe_get:
        from .smartlab_curl import fetch_smartlab_html

        return fetch_smartlab_html(url, observer=observer)
    started = time.monotonic()
    try:
        payload = fetch(url)
    except SourceUnavailable as exc:
        if not exc.source_url:
            exc.source_url = _diagnostic_url(url)
        raise
    if not isinstance(payload, (bytes, bytearray)):
        raise SourceUnavailable(
            "source reader returned a non-byte response",
            code="INVALID_RESPONSE", source_url=url,
        )
    raw = bytes(payload)
    _notify(observer, FetchDiagnostic(
        urlsplit(url).hostname or "", "complete", 1, time.monotonic() - started,
        len(raw), 200, "", _diagnostic_url(url),
    ))
    return raw


def _fetch_moex(
    url: str,
    fetch: Callable[[str], bytes],
    *,
    observer: Callable[[FetchDiagnostic], None] | None = None,
    timeout: float = _MAX_TIMEOUT_SECONDS,
    max_attempts: int = 2,
) -> tuple[bytes, int, float]:
    started = time.monotonic()
    events: list[FetchDiagnostic] = []

    def record(item: FetchDiagnostic) -> None:
        events.append(item)
        _notify(observer, item)

    if fetch is safe_get:
        payload = safe_get(url, timeout=timeout, max_attempts=max_attempts, observer=record)
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


def smartlab_robots_allow(
    fetch: Callable[[str], bytes] = safe_get,
    *,
    page_url: str = "https://smart-lab.ru/q/bonds/",
    robots_payload: bytes | None = None,
) -> bool:
    """Fail closed if robots.txt cannot be read or forbids the requested listing path."""
    url = "https://smart-lab.ru/robots.txt"
    target = urlsplit(page_url)
    if target.scheme != "https" or target.hostname != "smart-lab.ru" or target.username or target.password:
        raise SourceUnavailable(
            "Smart-Lab listing URL is outside the approved host",
            code="URL_NOT_ALLOWED", source_url=page_url,
        )
    raw = robots_payload if robots_payload is not None else _fetch_smartlab(url, fetch)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceUnavailable(
            "Smart-Lab robots file encoding", code="ROBOTS_INVALID_ENCODING",
            phase="robots", source_url=url,
        ) from exc
    if not text.strip():
        raise SourceUnavailable(
            "Smart-Lab robots policy is empty or unavailable",
            code="ROBOTS_EMPTY", phase="robots", source_url=url,
        )
    parser = RobotFileParser(url)
    parser.parse(text.splitlines())
    return parser.can_fetch(_USER_AGENT, page_url)


def discover_smartlab(
    *,
    fetch: Callable[[str], bytes] = safe_get,
    now: datetime | None = None,
    max_items: int = 100,
) -> list[Candidate]:
    robots_url = "https://smart-lab.ru/robots.txt"
    listing_url = "https://smart-lab.ru/q/bonds/"
    robots_raw = _fetch_smartlab(robots_url, fetch)
    if not smartlab_robots_allow(fetch, page_url=listing_url, robots_payload=robots_raw):
        raise SourceUnavailable(
            "Smart-Lab robots policy disallows the discovery path",
            code="ROBOTS_DISALLOWED", phase="robots", source_url=listing_url,
        )
    raw = _fetch_smartlab(listing_url, fetch)
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceUnavailable(
            "Smart-Lab response encoding", code="INVALID_ENCODING",
            phase="read", source_url=listing_url,
        ) from exc
    candidates = parse_smartlab(
        html, fetched_at=now or datetime.now(timezone.utc), max_items=max_items,
    )
    if not candidates:
        raise SourceUnavailable(
            "Smart-Lab page lacks ISIN-tagged rows",
            code="NO_ISIN_IDENTIFIERS", source_url=listing_url,
        )
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


def _moex_cursor(payload: bytes) -> tuple[int, int, int] | None:
    """Read ISS pagination metadata; return (index, total, page_size) if valid."""
    try:
        root = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(root, dict):
        return None
    cursor = root.get("securities.cursor") or root.get("cursor")
    securities = root.get("securities")
    if cursor is None and isinstance(securities, dict):
        cursor = securities.get("cursor")
    if not isinstance(cursor, dict):
        return None
    columns, rows = cursor.get("columns"), cursor.get("data")
    if not isinstance(columns, list) or not isinstance(rows, list) or not rows or not isinstance(rows[0], list):
        return None
    try:
        values = dict(zip((str(item).upper() for item in columns), rows[0]))
        index, total, page_size = (int(values[key]) for key in ("INDEX", "TOTAL", "PAGESIZE"))
    except (KeyError, TypeError, ValueError):
        return None
    if index < 0 or total < 0 or page_size < 0 or index > total:
        return None
    return index, total, page_size


def lookup_moex_isin(
    isin: str,
    *,
    fetch: Callable[[str], bytes] = safe_get,
    now: datetime | None = None,
    observer: Callable[[FetchDiagnostic], None] | None = None,
) -> MoexIsinCheck:
    """Check an ISIN exactly; a negative result is limited to verified scanned pages."""
    if not _ISIN.fullmatch(isin):
        raise ValueError("a Russian-format ISIN is required")
    started = time.monotonic()
    deadline = started + _MOEX_SCAN_BUDGET_SECONDS
    attempts = 0
    response_bytes = 0
    request_elapsed = 0.0
    scanned_pages = 0
    scanned_rows = 0
    scan_complete = False
    scan_ranges: list[str] = []
    expected_total: int | None = None
    match: dict[str, object] | None = None
    checked_url = ""

    def request(url: str) -> bytes:
        nonlocal attempts, response_bytes, request_elapsed
        remaining = deadline - time.monotonic()
        # Reserve one host-rate-limit interval and split the rest across the
        # socket connect/read timeout. The budget is checked between requests;
        # the current operation is synchronous and is the only possible overrun.
        if remaining <= _MIN_HOST_INTERVAL_SECONDS + 0.5:
            raise SourceUnavailable(
                "MOEX diagnostic budget exhausted", code="SCAN_BUDGET_EXHAUSTED",
                phase="budget", attempts=attempts, response_bytes=response_bytes,
                elapsed_seconds=time.monotonic() - started,
            )
        timeout = min(
            _MOEX_REQUEST_TIMEOUT_SECONDS,
            (remaining - _MIN_HOST_INTERVAL_SECONDS) / 2,
        )
        payload, used_attempts, elapsed = _fetch_moex(
            url, fetch, observer=observer, timeout=timeout, max_attempts=1,
        )
        attempts += used_attempts
        response_bytes += len(payload)
        request_elapsed += elapsed
        return payload

    finder_params = urlencode({
        "q": isin, "limit": 10, "iss.meta": "off", "iss.only": "securities",
    })
    finder_url = "https://iss.moex.com/iss/securities.json?" + finder_params
    finder_payload = request(finder_url)
    columns, rows = _moex_table(finder_payload)
    checked_url = finder_url
    for row in rows:
        if not isinstance(row, list):
            continue
        data = dict(zip(columns, row))
        if str(data.get("ISIN") or "").strip().upper() == isin:
            match = data
            break

    # The Security Finder may return no exact row even when the bond is listed.
    # Scan only a bounded number of official ISS pages and use its cursor to
    # prove the scanned range. A partial page set can never establish absence.
    if match is None:
        start = 0
        for _ in range(_MOEX_MAX_PAGE_COUNT):
            if time.monotonic() >= deadline:
                break
            page_params = urlencode({
                "iss.meta": "off",
                "iss.only": "securities,securities.cursor",
                "start": start,
                "limit": _MOEX_PAGE_LIMIT,
            })
            listing_url = (
                "https://iss.moex.com/iss/engines/stock/markets/bonds/securities.json?"
                + page_params
            )
            try:
                listing_payload = request(listing_url)
            except SourceUnavailable as exc:
                if exc.code == "SCAN_BUDGET_EXHAUSTED":
                    break
                raise
            listing_columns, listing_rows = _moex_table(listing_payload)
            scanned_pages += 1
            scanned_rows += len(listing_rows)
            cursor = _moex_cursor(listing_payload)

            for row in listing_rows:
                if not isinstance(row, list):
                    continue
                data = dict(zip(listing_columns, row))
                if str(data.get("ISIN") or "").strip().upper() == isin:
                    match = data
                    checked_url = listing_url
                    break

            if cursor is None:
                # Without ISS cursor metadata there is no proof of page coverage.
                break
            page_index, total, page_size = cursor
            scan_ranges.append(f"{page_index}:{page_index + page_size}")
            if page_index != start or page_size != len(listing_rows):
                break
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                break
            if page_index + page_size >= total:
                scan_complete = True
            if match is not None:
                checked_url = listing_url
                break
            if scan_complete:
                checked_url = listing_url
                break
            if page_size == 0 or not listing_rows:
                break
            start = page_index + page_size

    timestamp = now or datetime.now(timezone.utc)
    if match is not None:
        state = "IDENTIFIER_CONFIRMED_ON_MOEX"
    elif scan_complete:
        state = "NOT_FOUND_IN_SCANNED_RESULTS"
    else:
        state = "INCONCLUSIVE"
    scan_range = ",".join(scan_ranges)
    if expected_total is not None:
        scan_range = f"{scan_range};total={expected_total};complete={str(scan_complete).lower()}"
    return MoexIsinCheck(
        isin=isin,
        matched=match is not None,
        source_url=checked_url,
        fetched_at=timestamp,
        security_id=str((match or {}).get("SECID") or ""),
        short_name=str((match or {}).get("SHORTNAME") or (match or {}).get("SECNAME") or ""),
        state=state,
        attempts=attempts,
        response_bytes=response_bytes,
        elapsed_seconds=max(request_elapsed, time.monotonic() - started),
        scanned_pages=scanned_pages,
        scanned_rows=scanned_rows,
        scan_complete=scan_complete,
        scan_range=scan_range,
    )
