"""Bounded read-only discovery adapters. No claims of executable quotes or credit quality."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import re
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_ISIN = re.compile(r"(?<![A-Z0-9])RU[A-Z0-9]{10}(?![A-Z0-9])")
_MAX_BYTES = 3_000_000
_ALLOWED_HOSTS = frozenset({"smart-lab.ru", "iss.moex.com"})


class SourceUnavailable(RuntimeError):
    """A source cannot provide the required evidence; use another source."""


@dataclass(frozen=True)
class Candidate:
    isin: str
    name: str
    discovered_via: str
    source_url: str
    fetched_at: datetime
    status: str = "DISCOVERED_UNVERIFIED"

    def __post_init__(self) -> None:
        if not _ISIN.fullmatch(self.isin):
            raise ValueError("candidate must have a Russian-format ISIN")
        if self.fetched_at.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise SourceUnavailable("redirect blocked; source endpoint must be verified")


def safe_get(url: str, timeout: float = 10.0) -> bytes:
    """Only fixed public hosts, HTTPS, no cookies, no login, no redirects."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_HOSTS or parts.username or parts.password or parts.port:
        raise SourceUnavailable("non-allowlisted outbound address")
    request = Request(url, headers={"User-Agent": "MSPBonds-Research/0.1 (read-only; rate-limited by caller)"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            if urlsplit(response.geturl()).hostname != parts.hostname:
                raise SourceUnavailable("unexpected response host")
            raw = response.read(_MAX_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise SourceUnavailable(f"source request failed: {type(exc).__name__}") from exc
    if len(raw) > _MAX_BYTES:
        raise SourceUnavailable("oversized source response")
    return raw


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


def discover_smartlab(
    *, fetch: Callable[[str], bytes] = safe_get,
    now: datetime | None = None,
    max_items: int = 100,
) -> list[Candidate]:
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


def parse_moex(payload: bytes, *, fetched_at: datetime, max_items: int = 100) -> list[Candidate]:
    try:
        root = json.loads(payload)
        sec = root["securities"]
        columns = sec["columns"]
        rows = sec["data"]
        if not isinstance(columns, list) or not isinstance(rows, list):
            raise ValueError("invalid MOEX ISS table")
        unique: dict[str, Candidate] = {}
        for row in rows:
            data = dict(zip(columns, row))
            isin = str(data.get("ISIN") or data.get("SECID") or "")
            if not _ISIN.fullmatch(isin):
                continue
            name = str(data.get("SHORTNAME") or data.get("SECNAME") or data.get("NAME") or isin)
            unique.setdefault(
                isin,
                Candidate(isin=isin, name=name, discovered_via="MOEX ISS",
                          source_url=f"https://www.moex.com/ru/issue.aspx?code={isin}",
                          fetched_at=fetched_at),
            )
            if len(unique) >= max_items:
                break
        return list(unique.values())
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceUnavailable("invalid MOEX ISS response") from exc


def discover_moex(
    *, fetch: Callable[[str], bytes] = safe_get,
    now: datetime | None = None,
    max_items: int = 100,
) -> list[Candidate]:
    url = ("https://iss.moex.com/iss/engines/stock/markets/bonds/securities.json?"
           + urlencode({"iss.meta": "off", "iss.only": "securities", "start": 0}))
    result = parse_moex(fetch(url), fetched_at=now or datetime.now(timezone.utc), max_items=max_items)
    if not result:
        raise SourceUnavailable("MOEX ISS has no ISIN-tagged rows in first page")
    return result
