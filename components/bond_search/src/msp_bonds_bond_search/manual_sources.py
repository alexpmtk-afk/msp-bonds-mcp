"""Capture manually inspected facts only from a fresh, registered source.

No network access is performed here. Values supplied by an analyst remain
UNVERIFIED; missing values remain MISSING until independent review.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Mapping

from .catalog import PolicySourceSnapshot, SnapshotError
from .evidence import EvidenceItem, EvidenceKind, EvidenceState, normalize_rating_status
from .routes import EVIDENCE_ROUTES

RATING_CLAIMS = (
    "rating_grade",
    "rating_agency",
    "rating_scale",
    "rating_subject",
    "rating_subject_type",
    "rating_subject_id",
    "rating_action_date",
    "rating_action_type",
    "rating_status",
    "rating_action_is_latest",
)
ISSUE_TERMS_CLAIMS = (
    "coupon_legal_terms",
    "coupon_schedule",
    "amortization_terms",
    "offer_status",
    "future_coupon_terms_legally_defined",
    "issue_document_is_current",
)


def _registered_source(
    snapshot: PolicySourceSnapshot,
    category: str,
    source_name: str,
):
    if not isinstance(snapshot, PolicySourceSnapshot):
        raise ValueError("a current policy/source snapshot is required")
    try:
        snapshot.assert_fresh()
    except SnapshotError:
        raise ValueError("a current policy/source snapshot is required") from None
    if not isinstance(source_name, str) or source_name not in EVIDENCE_ROUTES[category]:
        raise ValueError("source is not registered for this evidence category")
    try:
        source = snapshot.resource(source_name)
    except SnapshotError:
        raise ValueError("source is absent from the current resource register") from None
    if not source.url.strip():
        raise ValueError("registered source URL is empty")
    return source


def _capture(
    claims: tuple[str, ...],
    values: Mapping[str, object],
    *,
    snapshot: PolicySourceSnapshot,
    source_name: str,
    category: str,
    kind: EvidenceKind,
    retrieved_at: datetime,
    document_date: date | None,
    locator: str,
) -> tuple[EvidenceItem, ...]:
    if not isinstance(values, Mapping):
        raise ValueError("captured values must be a mapping")
    if set(values) - set(claims):
        raise ValueError("captured values contain unknown claims")
    if retrieved_at.tzinfo is None:
        raise ValueError("retrieved_at must be timezone-aware")
    if document_date is not None and not isinstance(document_date, date):
        raise ValueError("document_date must be a date")
    source = _registered_source(snapshot, category, source_name)

    evidence = []
    for claim in claims:
        value = values.get(claim)
        if claim == "rating_status" and value is not None and value != "":
            value = normalize_rating_status(value)
        present = value is not None and value != "" and value != [] and value != {}
        evidence.append(EvidenceItem(
            claim=claim,
            value=value if present else None,
            unit="",
            source_name=source.name,
            source_url=source.url,
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
    snapshot: PolicySourceSnapshot,
    source_name: str,
    retrieved_at: datetime,
    document_date: date | None = None,
    locator: str = "",
) -> tuple[EvidenceItem, ...]:
    """Capture rating fields from a registered rating source; never confirms."""
    return _capture(
        RATING_CLAIMS, values,
        snapshot=snapshot,
        source_name=source_name,
        category="rating",
        kind=EvidenceKind.OFFICIAL,
        retrieved_at=retrieved_at,
        document_date=document_date,
        locator=locator,
    )


def capture_manual_issue_terms(
    values: Mapping[str, object],
    *,
    snapshot: PolicySourceSnapshot,
    source_name: str,
    retrieved_at: datetime,
    document_date: date | None = None,
    locator: str = "",
) -> tuple[EvidenceItem, ...]:
    """Capture issue terms from a registered primary source; never confirms."""
    return _capture(
        ISSUE_TERMS_CLAIMS, values,
        snapshot=snapshot,
        source_name=source_name,
        category="coupon_terms",
        kind=EvidenceKind.PRIMARY,
        retrieved_at=retrieved_at,
        document_date=document_date,
        locator=locator,
    )
