"""Bounded curl.exe transport and a short-lived, integrity-checked page cache."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

_HOST = "smart-lab.ru"
_MAX_HTML_BYTES = 3_000_000
_MAX_HEADER_BYTES = 65_536
_MAX_WRITEOUT_BYTES = 256
_READ_CHUNK_BYTES = 64 * 1024
_CONNECT_TIMEOUT_SECONDS = 10
_MAX_TRANSFER_SECONDS = 60
_MAX_FRESH_CACHE_SECONDS = 5 * 60
_MAX_ATTEMPTS = 2
_WRITEOUT_PREFIX = b"\n__MSP_SMARTLAB_META__"
_WRITEOUT_FORMAT = r"\n__MSP_SMARTLAB_META__%{http_code}\t%{size_download}\n"
_PAGE_SORT_RE = re.compile(r"/q/bonds/order_by_([a-z0-9_]+)/([a-z]+)/page\d+", re.I)
_ETAG_RE = re.compile(r"(?:W/)?\"[\x21\x23-\x7e]*\"\Z")


@dataclass(frozen=True)
class _CachedPage:
    url: str
    body: bytes
    saved_at: datetime
    validated_at: datetime
    etag: str
    last_modified: str
    sort_signature: str
    content_length: int | None
    transfer_encoding: str
    content_encoding: str

    def age_seconds(self, now: datetime) -> float:
        return (now - self.validated_at).total_seconds()


@dataclass(frozen=True)
class _CurlResponse:
    body: bytes
    status: int
    wire_bytes: int | None
    headers: dict[str, str]
    elapsed_seconds: float
    exit_code: int


def _safe_url(url: str):
    from .discovery import SourceUnavailable

    try:
        parts = urlsplit(url)
        port = parts.port
    except (TypeError, ValueError):
        parts, port = None, None
    if (
        parts is None
        or parts.scheme != "https"
        or parts.hostname != _HOST
        or parts.username or parts.password or port
        or parts.fragment
        or any(ord(char) < 32 for char in url)
        or not (parts.path == "/robots.txt" or parts.path.startswith("/q/bonds/"))
    ):
        raise SourceUnavailable(
            "curl transport accepts only fixed Smart-Lab HTTPS paths",
            code="URL_NOT_ALLOWED", phase="validate", source_url=url,
        )
    return parts


def _sort_signature(url: str, body: bytes) -> str:
    parts = urlsplit(url)
    path_match = _PAGE_SORT_RE.search(parts.path)
    if path_match:
        return f"{path_match.group(1).lower()}:{path_match.group(2).lower()}"
    if parts.path.rstrip("/") != "/q/bonds":
        return "unrecognized"
    html = body.decode("utf-8", errors="ignore")
    signatures = {
        f"{field.lower()}:{direction.lower()}"
        for field, direction in _PAGE_SORT_RE.findall(html)
    }
    if len(signatures) == 1:
        return next(iter(signatures))
    if len(signatures) > 1:
        return "ambiguous:" + ",".join(sorted(signatures))
    return "default"


def _default_cache_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "MspBondsMcp" / "SmartLabPageCache"


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _safe_validator(value: str, *, etag: bool) -> str:
    value = value.strip()
    if not value or len(value) > 512 or any(ord(c) < 32 or ord(c) > 126 for c in value):
        return ""
    if etag:
        return value if _ETAG_RE.fullmatch(value) else ""
    try:
        parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return ""
    return value


class SmartLabPageCache:
    """Atomic page snapshots; fallback is allowed only for fresh, URL/sort-matched data."""

    def __init__(self, root: Path | str, *, max_fallback_age_seconds: int = _MAX_FRESH_CACHE_SECONDS):
        self.root = Path(root)
        self.max_fallback_age_seconds = max_fallback_age_seconds

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.root / f"{key}.html", self.root / f"{key}.json"

    def load(self, url: str) -> _CachedPage | None:
        try:
            body_path, metadata_path = self._paths(url)
            if metadata_path.stat().st_size > 16_384 or body_path.stat().st_size > _MAX_HTML_BYTES:
                return None
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            body = body_path.read_bytes()
            if (
                metadata.get("version") != 1
                or metadata.get("url") != url
                or metadata.get("sha256") != hashlib.sha256(body).hexdigest()
                or metadata.get("size_bytes") != len(body)
            ):
                return None
            saved_at = _parse_utc(metadata.get("saved_at"))
            validated_at = _parse_utc(metadata.get("validated_at"))
            sort_signature = metadata.get("sort_signature")
            if (
                saved_at is None or validated_at is None
                or not isinstance(sort_signature, str)
                or sort_signature == "unrecognized"
                or sort_signature.startswith("ambiguous:")
                or _sort_signature(url, body) != sort_signature
            ):
                return None
            etag = _safe_validator(str(metadata.get("etag") or ""), etag=True)
            last_modified = _safe_validator(str(metadata.get("last_modified") or ""), etag=False)
            content_length_value = metadata.get("content_length")
            content_length = (
                content_length_value
                if isinstance(content_length_value, int) and content_length_value >= 0
                else None
            )
            transfer_encoding = str(metadata.get("transfer_encoding") or "")[:80]
            content_encoding = str(metadata.get("content_encoding") or "")[:80]
            return _CachedPage(
                url, body, saved_at, validated_at, etag, last_modified,
                sort_signature, content_length, transfer_encoding, content_encoding,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def save(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        *,
        now: datetime | None = None,
    ) -> bool:
        if len(body) > _MAX_HTML_BYTES:
            return False
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        signature = _sort_signature(url, body)
        if signature == "unrecognized" or signature.startswith("ambiguous:"):
            return False
        content_length_text = headers.get("content-length", "")
        content_length = int(content_length_text) if content_length_text.isdigit() else None
        metadata = {
            "version": 1,
            "url": url,
            "saved_at": timestamp.isoformat(),
            "validated_at": timestamp.isoformat(),
            "sha256": hashlib.sha256(body).hexdigest(),
            "size_bytes": len(body),
            "sort_signature": signature,
            "etag": _safe_validator(headers.get("etag", ""), etag=True),
            "last_modified": _safe_validator(headers.get("last-modified", ""), etag=False),
            "content_length": content_length,
            "transfer_encoding": headers.get("transfer-encoding", "")[:80],
            "content_encoding": headers.get("content-encoding", "")[:80],
        }
        self.root.mkdir(parents=True, exist_ok=True)
        body_path, metadata_path = self._paths(url)
        suffix = f".{os.getpid()}.{threading.get_ident()}.tmp"
        body_tmp = body_path.with_suffix(body_path.suffix + suffix)
        metadata_tmp = metadata_path.with_suffix(metadata_path.suffix + suffix)
        try:
            body_tmp.write_bytes(body)
            metadata_tmp.write_text(
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(body_tmp, body_path)
            os.replace(metadata_tmp, metadata_path)
        finally:
            body_tmp.unlink(missing_ok=True)
            metadata_tmp.unlink(missing_ok=True)
        return True

    def revalidate(self, entry: _CachedPage, *, now: datetime | None = None) -> None:
        _, metadata_path = self._paths(entry.url)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["validated_at"] = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        tmp = metadata_path.with_suffix(metadata_path.suffix + f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, metadata_path)
        finally:
            tmp.unlink(missing_ok=True)

    def is_fresh(self, entry: _CachedPage, *, now: datetime | None = None) -> bool:
        timestamp = now or datetime.now(timezone.utc)
        age = entry.age_seconds(timestamp)
        return 0 <= age <= self.max_fallback_age_seconds


def _curl_executable() -> str | None:
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        system_curl = Path(system_root) / "System32" / "curl.exe"
        if system_curl.is_file():
            return str(system_curl)
    found = shutil.which("curl.exe")
    return str(Path(found).resolve()) if found else None


def _parse_header_dump(path: Path) -> dict[str, str]:
    try:
        if path.stat().st_size > _MAX_HEADER_BYTES:
            return {"_parse_error": "HEADERS_TOO_LARGE"}
        lines = path.read_bytes().decode("latin-1").splitlines()
    except OSError:
        return {}
    blocks: list[list[str]] = []
    for line in lines:
        if re.match(r"^HTTP/\S+\s+\d{3}(?:\s|$)", line, re.I):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    if not blocks:
        return {}
    headers: dict[str, str] = {}
    for line in blocks[-1][1:]:
        if not line or line[:1].isspace() or ":" not in line:
            continue
        name, value = line.split(":", 1)
        key = name.strip().lower()
        if key in {"content-length", "transfer-encoding", "content-encoding", "etag", "last-modified"}:
            headers[key] = value.strip()[:512]
    return headers


def _status_from_headers(path: Path) -> int | None:
    try:
        if path.stat().st_size > _MAX_HEADER_BYTES:
            return None
        lines = path.read_bytes().decode("latin-1").splitlines()
    except OSError:
        return None
    statuses = [
        int(match.group(1)) for line in lines
        if (match := re.match(r"^HTTP/\S+\s+(\d{3})(?:\s|$)", line, re.I))
    ]
    return statuses[-1] if statuses else None


def _run_curl(
    url: str,
    *,
    attempt: int,
    conditional_headers: tuple[str, ...],
    observer: Callable[[object], None] | None,
) -> _CurlResponse:
    from .discovery import FetchDiagnostic, SourceUnavailable, _rate_limit

    _safe_url(url)
    executable = _curl_executable()
    if not executable:
        raise SourceUnavailable("curl.exe is unavailable", code="CURL_NOT_FOUND", phase="start", source_url=url)
    _rate_limit(_HOST)
    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="mspbonds-smartlab-") as temp_dir:
        header_path = Path(temp_dir) / "response.headers"
        args = [
            executable, "--disable", "--silent", "--show-error", "--compressed",
            "--proto", "=https", "--connect-timeout", str(_CONNECT_TIMEOUT_SECONDS),
            "--max-time", str(_MAX_TRANSFER_SECONDS), "--max-filesize", str(_MAX_HTML_BYTES),
            "--user-agent", "MSPBonds-Research/0.1",
            "--header", "Accept: text/html,application/json;q=0.9,*/*;q=0.5",
            "--dump-header", str(header_path), "--output", "-",
            "--write-out", _WRITEOUT_FORMAT,
        ]
        for header in conditional_headers:
            args.extend(("--header", header))
        args.extend(("--url", url))
        spool = tempfile.TemporaryFile(mode="w+b")
        overflow = threading.Event()
        reader_errors: list[BaseException] = []
        process = None
        timed_out = False

        try:
            process = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, shell=False, close_fds=True,
            )
        except OSError as exc:
            spool.close()
            code = "CURL_NOT_FOUND" if isinstance(exc, FileNotFoundError) else "CURL_START_FAILED"
            raise SourceUnavailable(
                "curl.exe could not be started", code=code, phase="start", source_url=url,
            ) from None

        def drain_output() -> None:
            try:
                stdout = process.stdout
                reader = getattr(stdout, "read1", None)
                if not callable(reader):
                    reader = stdout.read
                while True:
                    block = reader(_READ_CHUNK_BYTES)
                    if not block:
                        break
                    if spool.tell() + len(block) > _MAX_HTML_BYTES + _MAX_WRITEOUT_BYTES:
                        overflow.set()
                        try:
                            process.kill()
                        except OSError:
                            pass
                        break
                    spool.write(block)
            except BaseException as exc:  # surfaced safely after process termination
                reader_errors.append(exc)
                try:
                    process.kill()
                except OSError:
                    pass

        thread = threading.Thread(target=drain_output, name="smartlab-curl-output", daemon=True)
        thread.start()
        try:
            return_code = process.wait(timeout=_MAX_TRANSFER_SECONDS + 5)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            try:
                return_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return_code = process.returncode
        thread.join(timeout=5)
        if thread.is_alive():
            try:
                process.kill()
                process.stdout.close()
            except OSError:
                pass
            thread.join(timeout=1)
        spool.seek(0)
        captured = spool.read(_MAX_HTML_BYTES + _MAX_WRITEOUT_BYTES + 1)
        spool.close()
        elapsed = time.monotonic() - started
        headers = _parse_header_dump(header_path)
        status = _status_from_headers(header_path)
        content_length_text = headers.get("content-length", "")
        content_length = int(content_length_text) if content_length_text.isdigit() else None
        transfer_encoding = re.sub(r"[^a-z0-9,._-]", "", headers.get("transfer-encoding", "").lower())[:80]
        content_encoding = re.sub(r"[^a-z0-9,._-]", "", headers.get("content-encoding", "").lower())[:80]
        body_framing = "chunked" if "chunked" in transfer_encoding else (
            "content-length" if content_length is not None else "connection-close"
        )

        body, writeout_status, wire_bytes = _split_writeout(captured)
        if writeout_status is not None:
            status = writeout_status

        def record_failure(code: str, *, retryable: bool) -> None:
            if observer is not None:
                observer(FetchDiagnostic(
                    _HOST, "transfer", attempt, elapsed, len(body), status, code, url,
                    content_length, transfer_encoding, content_encoding,
                    body_framing, False, wire_bytes, return_code,
                ))

        if overflow.is_set() or len(body) > _MAX_HTML_BYTES:
            record_failure("RESPONSE_TOO_LARGE", retryable=False)
            raise SourceUnavailable(
                "decompressed Smart-Lab HTML exceeded the 3 MB limit",
                code="RESPONSE_TOO_LARGE", phase="read", http_status=status,
                attempts=attempt, response_bytes=min(len(body), _MAX_HTML_BYTES + 1),
                elapsed_seconds=elapsed, source_url=url,
            )
        if timed_out or thread.is_alive():
            record_failure("CURL_TIMEOUT", retryable=True)
            raise SourceUnavailable(
                "curl transfer exceeded its bounded time limit",
                code="CURL_TIMEOUT", retryable=True, phase="read",
                http_status=status, attempts=attempt, response_bytes=len(body),
                elapsed_seconds=elapsed, source_url=url,
            )
        if reader_errors:
            record_failure("CURL_OUTPUT_READ_FAILED", retryable=True)
            raise SourceUnavailable(
                "curl response output could not be read",
                code="CURL_OUTPUT_READ_FAILED", retryable=True, phase="read",
                http_status=status, attempts=attempt, response_bytes=len(body),
                elapsed_seconds=elapsed, source_url=url,
            )
        if return_code != 0:
            code = "CURL_TIMEOUT" if return_code == 28 else (
                "RESPONSE_TOO_LARGE" if return_code == 63 else (
                "CURL_INCOMPLETE_TRANSFER" if return_code == 18 else f"CURL_EXIT_{return_code}"
                )
            )
            retryable = return_code in {5, 6, 7, 18, 28, 52, 55, 56, 92}
            record_failure(code, retryable=retryable)
            raise SourceUnavailable(
                "curl reported an unsuccessful transfer",
                code=code, retryable=retryable, http_status=status,
                phase="read", attempts=attempt, response_bytes=len(body),
                elapsed_seconds=elapsed, source_url=url,
            )
        if writeout_status is None or wire_bytes is None:
            record_failure("CURL_WRITEOUT_MISSING", retryable=True)
            raise SourceUnavailable(
                "curl did not provide its completion metadata",
                code="CURL_WRITEOUT_MISSING", retryable=True,
                http_status=status, phase="read", attempts=attempt,
                response_bytes=len(body), elapsed_seconds=elapsed, source_url=url,
            )
        if status not in {200, 304}:
            code = f"HTTP_{status}" if status is not None else "HTTP_STATUS_MISSING"
            retryable = status in {500, 502, 503, 504}
            record_failure(code, retryable=retryable)
            raise SourceUnavailable(
                "Smart-Lab returned a non-success HTTP status",
                code=code, retryable=retryable, http_status=status,
                phase="http", attempts=attempt, response_bytes=len(body),
                elapsed_seconds=elapsed, source_url=url,
            )
        return _CurlResponse(body, int(status), wire_bytes, headers, elapsed, int(return_code))


def _split_writeout(output: bytes) -> tuple[bytes, int | None, int | None]:
    marker = output.rfind(_WRITEOUT_PREFIX)
    if marker < 0:
        return output, None, None
    body = output[:marker]
    trailer = output[marker + len(_WRITEOUT_PREFIX):]
    if not trailer.endswith(b"\n"):
        return body, None, None
    fields = trailer[:-1].split(b"\t", 1)
    if len(fields) != 2 or not fields[0].isdigit() or not re.fullmatch(rb"\d+(?:\.\d+)?", fields[1]):
        return body, None, None
    return body, int(fields[0]), int(float(fields[1]))


def _emit(
    observer,
    *,
    url: str,
    attempt: int,
    phase: str,
    started: float,
    body: bytes,
    status: int | None,
    headers: dict[str, str],
    code: str = "",
    wire_bytes: int | None = None,
    curl_exit_code: int | None = None,
    cache_status: str = "",
    cache_age_seconds: float | None = None,
    content_fetched_at: datetime | None = None,
) -> None:
    if observer is None:
        return
    from .discovery import FetchDiagnostic

    content_length_text = headers.get("content-length", "")
    content_length = int(content_length_text) if content_length_text.isdigit() else None
    transfer_encoding = re.sub(r"[^a-z0-9,._-]", "", headers.get("transfer-encoding", "").lower())[:80]
    content_encoding = re.sub(r"[^a-z0-9,._-]", "", headers.get("content-encoding", "").lower())[:80]
    framing = "chunked" if "chunked" in transfer_encoding else (
        "content-length" if content_length is not None else "connection-close"
    )
    observer(FetchDiagnostic(
        _HOST, phase, attempt, time.monotonic() - started, len(body), status,
        code, url, content_length, transfer_encoding, content_encoding,
        framing, not bool(code), wire_bytes, curl_exit_code, cache_status,
        cache_age_seconds,
        content_fetched_at.astimezone(timezone.utc).isoformat() if content_fetched_at else "",
    ))


_DEFAULT_CACHE: SmartLabPageCache | None = None
_DEFAULT_CACHE_LOCK = threading.Lock()


def _default_cache() -> SmartLabPageCache:
    global _DEFAULT_CACHE
    with _DEFAULT_CACHE_LOCK:
        if _DEFAULT_CACHE is None:
            _DEFAULT_CACHE = SmartLabPageCache(_default_cache_root())
        return _DEFAULT_CACHE


def fetch_smartlab_html(
    url: str,
    *,
    observer: Callable[[object], None] | None = None,
    cache: SmartLabPageCache | None = None,
) -> bytes:
    """Fetch one Smart-Lab page with curl, bounded output, retries, and safe resume."""
    from .discovery import SourceUnavailable

    parts = _safe_url(url)
    cache = cache or _default_cache()
    is_listing_page = parts.path.startswith("/q/bonds/")
    cached = cache.load(url) if is_listing_page else None
    conditional_headers: list[str] = []
    if cached is not None:
        if cached.etag:
            conditional_headers.append(f"If-None-Match: {cached.etag}")
        if cached.last_modified:
            conditional_headers.append(f"If-Modified-Since: {cached.last_modified}")

    last_error: SourceUnavailable | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            response = _run_curl(
                url, attempt=attempt,
                conditional_headers=tuple(conditional_headers), observer=observer,
            )
        except SourceUnavailable as exc:
            last_error = exc
            if not exc.retryable or attempt >= _MAX_ATTEMPTS:
                break
            time.sleep(0.25)
            continue

        now = datetime.now(timezone.utc)
        if response.status == 304:
            if cached is None:
                last_error = SourceUnavailable(
                    "Smart-Lab returned 304 without a verified cached page",
                    code="CACHE_REVALIDATION_MISS", phase="cache",
                    http_status=304, source_url=url,
                )
                break
            try:
                cache.revalidate(cached, now=now)
            except OSError:
                pass
            _emit(
                observer, url=url, attempt=attempt, phase="cache", started=started,
                body=cached.body, status=304,
                headers=response.headers, wire_bytes=response.wire_bytes,
                curl_exit_code=response.exit_code, cache_status="REVALIDATED",
                cache_age_seconds=0.0, content_fetched_at=now,
            )
            return cached.body

        cache_status = "DISABLED"
        if is_listing_page:
            try:
                cache_status = (
                    "STORED" if cache.save(url, response.body, response.headers, now=now)
                    else "SKIPPED_SORT_VALIDATION"
                )
            except OSError:
                cache_status = "WRITE_FAILED"
        _emit(
            observer, url=url, attempt=attempt, phase="complete", started=started,
            body=response.body, status=response.status,
            headers=response.headers, wire_bytes=response.wire_bytes,
            curl_exit_code=response.exit_code, cache_status=cache_status,
            cache_age_seconds=0.0, content_fetched_at=now,
        )
        return response.body

    now = datetime.now(timezone.utc)
    if (
        is_listing_page and cached is not None and last_error is not None
        and last_error.retryable and cache.is_fresh(cached, now=now)
    ):
        age = cached.age_seconds(now)
        _emit(
            observer, url=url, attempt=last_error.attempts, phase="cache", started=time.monotonic(),
            body=cached.body, status=200,
            headers={
                "content-length": str(cached.content_length or len(cached.body)),
                "transfer-encoding": cached.transfer_encoding,
                "content-encoding": cached.content_encoding,
            },
            cache_status="FRESH_FALLBACK", cache_age_seconds=age,
            content_fetched_at=cached.saved_at,
        )
        return cached.body
    if last_error is None:
        last_error = SourceUnavailable(
            "curl transport did not produce a response", code="CURL_NO_RESULT", source_url=url,
        )
    raise last_error
