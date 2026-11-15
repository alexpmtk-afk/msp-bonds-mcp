"""Official-source acquisition capabilities for the isolated bond-search component.

Only the documented MOEX ISS security finder is automated here. Other providers
remain explicit manual/authenticated routes until their official machine API,
terms, and access are verified. No missing field is inferred.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .discovery import FetchDiagnostic, MoexIsinCheck, SourceUnavailable, lookup_moex_isin, safe_get
from .evidence import EvidenceItem, EvidenceKind, EvidenceState


@dataclass(frozen=True)
class OfficialProviderCapability:
    field: str
    provider: str
    url: str
    mode: str
    status: str
    note: str


OFFICIAL_PROVIDER_CAPABILITIES: tuple[OfficialProviderCapability, ...] = (
    OfficialProviderCapability(
        "identity", "MOEX ISS", "https://iss.moex.com/iss/securities.json",
        "public_machine_api", "IMPLEMENTED_IDENTIFIER_ONLY",
        "Exact ISIN lookup confirms only returned identifier and MOEX code/name.",
    ),
    OfficialProviderCapability(
        "rating", "Банк России — рейтинговый репозитарий", "https://ratings.cbr.ru/",
        "official_web_search", "MANUAL_SOURCE_ONLY",
        "No documented anonymous machine endpoint was verified; no page scraping is used.",
    ),
    OfficialProviderCapability(
        "coupon_terms", "Официальное решение о выпуске / эмитент / НРД",
        "https://www.moex.com/ru/bondization", "official_documents",
        "MANUAL_SOURCE_ONLY",
        "Exchange event fields are not substituted for legally binding issue terms.",
    ),
    OfficialProviderCapability(
        "amortization", "MOEX ISS / официальное решение о выпуске",
        "https://www.moex.com/ru/bondization", "official_market_data_and_documents",
        "MANUAL_SOURCE_ONLY",
        "A schedule is not confirmed until an official instrument record is parsed and cross-checked.",
    ),
    OfficialProviderCapability(
        "default_events", "Федресурс / картотека арбитражных дел",
        "https://fedresurs.ru/", "official_web_search", "MANUAL_SOURCE_ONLY",
        "No undocumented endpoint or incomplete search is treated as a clean result.",
    ),
    OfficialProviderCapability(
        "financial_condition", "ФНС — ресурс БФО",
        "https://bo.nalog.gov.ru/", "public_web_search_and_subscribed_rest_api",
        "REST_API_ACCESS_REQUIRED",
        "The official REST service requires a subscription; HOME has no provider credentials.",
    ),
)


@dataclass(frozen=True)
class OfficialIdentifierResult:
    isin: str
    provider: str
    status: str
    evidence: tuple[EvidenceItem, ...]
    security_id: str = ""
    short_name: str = ""
    attempts: int = 0
    response_bytes: int = 0
    elapsed_seconds: float = 0.0
    scanned_pages: int = 0
    scanned_rows: int = 0
    scan_complete: bool = False
    scan_range: str = ""


def load_moex_identifier(
    isin: str,
    *,
    fetch: Callable[[str], bytes] = safe_get,
    now: datetime | None = None,
    observer: Callable[[FetchDiagnostic], None] | None = None,
) -> OfficialIdentifierResult:
    """Load only an exact ISIN finder result; this is not full issuer verification."""
    checked: MoexIsinCheck = lookup_moex_isin(
        isin, fetch=fetch, now=now, observer=observer,
    )
    evidence: tuple[EvidenceItem, ...] = ()
    status = checked.state
    if checked.matched:
        evidence = (
            EvidenceItem(
                claim="isin",
                value=checked.isin,
                unit="",
                source_name="MOEX ISS",
                source_url=checked.source_url,
                kind=EvidenceKind.OFFICIAL,
                state=EvidenceState.CONFIRMED,
                retrieved_at=checked.fetched_at,
                method="exact_ISIN_match",
                note="Confirms only the identifier; issuer, terms, ratings and payment facts remain unchecked.",
            ),
        )
        status = "IDENTIFIER_CONFIRMED_ONLY"
    return OfficialIdentifierResult(
        isin=checked.isin,
        provider="MOEX ISS",
        status=status,
        evidence=evidence,
        security_id=checked.security_id,
        short_name=checked.short_name,
        attempts=checked.attempts,
        response_bytes=checked.response_bytes,
        elapsed_seconds=checked.elapsed_seconds,
        scanned_pages=checked.scanned_pages,
        scanned_rows=checked.scanned_rows,
        scan_complete=checked.scan_complete,
        scan_range=checked.scan_range,
    )


def provider_capabilities() -> tuple[OfficialProviderCapability, ...]:
    """Return machine-readable implementation limits without exposing user policy."""
    return OFFICIAL_PROVIDER_CAPABILITIES
