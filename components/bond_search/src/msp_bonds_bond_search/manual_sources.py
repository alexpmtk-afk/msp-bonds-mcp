"""Capture official rating and issue-document facts without inventing verification.

This module performs no network access. Callers must select a source from the
approved resource register and inspect the referenced official record. Supplied
values remain UNVERIFIED; omitted required fields remain MISSING.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Mapping

from .evidence import EvidenceItem, EvidenceKind, EvidenceState

RATING_CLAIMS = (
    "rating_grade",
    "rating_agency",
    "rating_scale",
    "rating_subject",
    "rating_action_date",
)
ISSUE_TERMS_CLAIMS = (
    "coupon_legal_terms",
    "coupon_schedule",
    "amortization_terms",
    "offer_status",
)


def _capture(
    claims: tuple[str, ...],
    values: Mapping[str, object],
    *,
    source_name: str,
    source_url: str,
    kind: EvidenceKind,
    retrieved_at: datetime,
    document_date: date | None,
    locator: str,
) -> tuple[EvidenceItem, ...]:
    if not isinstance(values, Mapping):
        raise ValueError("captured values must be a mapping")
    extra = set(values) - set(claims)
    if extra:
        raise ValueError("captured values contain unknown claims")
    if retrieved_at.tzinfo is None:
        raise ValueError("retrieved_at must be timezone-aware")
    if document_date is not None and not isinstance(document_date, date):
        raise ValueError("document_date must be a date")
    if not source_name.strip():
        raise ValueError("source name is required")

    evidence = []
    for claim in claims:
        value = values.get(claim)
        present = value is not None and value != "" and value != [] and value != {}
        evidence.append(EvidenceItem(
            claim=claim,
            value=value if present else None,
            unit="",
            source_name=source_name,
            source_url=source_url,
            kind=kind,
            state=EvidenceState.UNVERIFIED if present else EvidenceState.MISSING,
            retrieved_at=retrieved_at,
            document_date=document_date,
            method="manual_official_record_capture",
            note=locator,
        ))
    return tuple(evidence)


def capture_manual_rating(
    values: Mapping[str, object],
    *,
    source_name: str,
    source_url: str,
    retrieved_at: datetime,
    document_date: date | None = None,
    locator: str = "",
) -> tuple[EvidenceItem, ...]:
    """Capture the five required rating fields; never marks them confirmed."""
    return _capture(
        RATING_CLAIMS, values,
        source_name=source_name,
        source_url=source_url,
        kind=EvidenceKind.OFFICIAL,
        retrieved_at=retrieved_at,
        document_date=document_date,
        locator=locator,
    )


def capture_manual_issue_terms(
    values: Mapping[str, object],
    *,
    source_name: str,
    source_url: str,
    retrieved_at: datetime,
    document_date: date | None = None,
    locator: str = "",
) -> tuple[EvidenceItem, ...]:
    """Capture legal coupon/redemption facts; never marks them confirmed."""
    return _capture(
        ISSUE_TERMS_CLAIMS, values,
        source_name=source_name,
        source_url=source_url,
        kind=EvidenceKind.PRIMARY,
        retrieved_at=retrieved_at,
        document_date=document_date,
        locator=locator,
    )
