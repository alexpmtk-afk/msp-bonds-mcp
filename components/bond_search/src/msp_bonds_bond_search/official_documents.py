"""Bounded provenance capture for an explicitly supplied official document.

This module does not crawl websites, call undocumented APIs, or extract legal
facts from PDFs automatically. A caller supplies bytes from a file or a
previously retrieved exact URL and manually transcribed fields with locators.
The payload hash proves which bytes were reviewed; it does not prove content.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from hashlib import sha256
import unicodedata
from typing import Mapping
from urllib.parse import urlsplit

from .catalog import PolicySourceSnapshot, SnapshotError
from .evidence import (
    DocumentProvenance,
    EvidenceItem,
    EvidenceKind,
    EvidenceState,
    normalize_rating_status,
)
from .manual_sources import ISSUE_TERMS_CLAIMS, RATING_CLAIMS
from .routes import EVIDENCE_ROUTES

MAX_DOCUMENT_BYTES = 25_000_000


class DocumentCaptureStatus(str, Enum):
    CAPTURED_REVIEW_REQUIRED = "DOCUMENT_CAPTURED_REVIEW_REQUIRED"
    CAPTURED_CURRENTNESS_UNKNOWN = "DOCUMENT_CAPTURED_CURRENTNESS_UNKNOWN"
    IDENTITY_MISMATCH = "DOCUMENT_IDENTITY_MISMATCH"
    NOT_CURRENT = "DOCUMENT_MARKED_SUPERSEDED_OR_WITHDRAWN"


@dataclass(frozen=True)
class OfficialDocumentCapture:
    status: DocumentCaptureStatus
    provenance: DocumentProvenance
    evidence: tuple[EvidenceItem, ...]


_DOCUMENT_CATEGORIES: Mapping[str, str] = {
    "RATING_RELEASE": "rating",
    "RATING_REPOSITORY_ENTRY": "rating",
    "ISSUE_DECISION": "coupon_terms",
    "PROSPECTUS": "coupon_terms",
    "COUPON_NOTICE": "coupon_terms",
    "AMORTIZATION_NOTICE": "coupon_terms",
    "OFFER_NOTICE": "coupon_terms",
    "ISSUE_AMENDMENT": "coupon_terms",
}


def capture_official_document(
    *,
    snapshot: PolicySourceSnapshot,
    source_name: str,
    document_type: str,
    document_id: str,
    issuer: str,
    isin: str,
    issue_registration_number: str,
    document_date: date,
    retrieved_at: datetime,
    artifact_bytes: bytes,
    expected_isin: str = "",
    expected_issuer: str = "",
    expected_issue_registration_number: str = "",
    document_url: str = "",
    locator: str = "",
    field_locators: Mapping[str, str] | None = None,
    values: Mapping[str, object] | None = None,
    source_status: str = "UNKNOWN",
) -> OfficialDocumentCapture:
    """Attach manual field extraction to exact supplied document bytes.

    The source must be present in the fresh canonical register and allowed for
    the document category. If target identity mismatches, no claim evidence is
    emitted. Currentness is never inferred from age; it must be established
    from the official source's latest record or amendment chain.
    """
    category = _DOCUMENT_CATEGORIES.get(document_type)
    if category is None:
        raise ValueError("unsupported official document type")
    source = _registered_source(snapshot, category, source_name)
    content = bytes(artifact_bytes) if isinstance(artifact_bytes, (bytes, bytearray)) else b""
    if not content:
        raise ValueError("official document bytes are required")
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError("official document exceeds the size limit")
    if not (expected_isin or expected_issuer.strip() or expected_issue_registration_number.strip()):
        raise ValueError("an expected issuer or issue identifier is required")
    direct_url = document_url.strip() or source.url
    _validate_document_url(direct_url, source.url)

    values = {} if values is None else values
    if not isinstance(values, Mapping):
        raise ValueError("manually extracted document fields must be a mapping")
    allowed_claims = RATING_CLAIMS if category == "rating" else ISSUE_TERMS_CLAIMS
    if set(values) - set(allowed_claims):
        raise ValueError("document fields contain unknown claims")
    locators = {} if field_locators is None else field_locators
    if not isinstance(locators, Mapping) or any(not isinstance(value, str) for value in locators.values()):
        raise ValueError("document field locators must be text")
    if set(locators) - set(allowed_claims):
        raise ValueError("document locators contain unknown claims")
    for claim, value in values.items():
        if value is not None and value != "" and value != [] and value != {}:
            if not (locators.get(claim, "").strip() or locator.strip()):
                raise ValueError("each extracted value requires a page or section locator")

    provenance = DocumentProvenance(
        document_type=document_type,
        document_id=document_id,
        issuer=issuer,
        isin=isin,
        issue_registration_number=issue_registration_number,
        document_date=document_date,
        source_name=source.name,
        source_url=direct_url,
        retrieved_at=retrieved_at,
        artifact_sha256=sha256(content).hexdigest(),
        locator=locator,
        source_status=source_status,
    )

    if not _identity_matches(
        isin=isin,
        issuer=issuer,
        issue_registration_number=issue_registration_number,
        expected_isin=expected_isin,
        expected_issuer=expected_issuer,
        expected_issue_registration_number=expected_issue_registration_number,
    ):
        return OfficialDocumentCapture(DocumentCaptureStatus.IDENTITY_MISMATCH, provenance, ())
    if source_status in {"SUPERSEDED", "WITHDRAWN"}:
        return OfficialDocumentCapture(DocumentCaptureStatus.NOT_CURRENT, provenance, ())

    claims = RATING_CLAIMS if category == "rating" else ISSUE_TERMS_CLAIMS
    evidence_kind = EvidenceKind.OFFICIAL if category == "rating" else EvidenceKind.PRIMARY
    evidence = []
    for claim in claims:
        value = values.get(claim)
        if category == "rating" and claim == "rating_status" and value is not None and value != "":
            value = normalize_rating_status(value)
        present = value is not None and value != "" and value != [] and value != {}
        evidence.append(EvidenceItem(
            claim=claim,
            value=value if present else None,
            unit="",
            source_name=source.name,
            source_url=direct_url,
            kind=evidence_kind,
            state=EvidenceState.UNVERIFIED if present else EvidenceState.MISSING,
            retrieved_at=retrieved_at,
            document_date=document_date,
            method="manual_official_document_capture",
            note=locators.get(claim, locator),
            document_provenance=provenance,
        ))
    status = (
        DocumentCaptureStatus.CAPTURED_REVIEW_REQUIRED
        if source_status == "CURRENT"
        else DocumentCaptureStatus.CAPTURED_CURRENTNESS_UNKNOWN
    )
    return OfficialDocumentCapture(status, provenance, tuple(evidence))


def _registered_source(snapshot: PolicySourceSnapshot, category: str, source_name: str):
    if not isinstance(snapshot, PolicySourceSnapshot):
        raise ValueError("a current policy/source snapshot is required")
    try:
        snapshot.assert_fresh()
    except SnapshotError:
        raise ValueError("a current policy/source snapshot is required") from None
    if not isinstance(source_name, str) or source_name not in EVIDENCE_ROUTES[category]:
        raise ValueError("source is not registered for this document category")
    try:
        source = snapshot.resource(source_name)
    except SnapshotError:
        raise ValueError("source is absent from the current resource register") from None
    if not source.url.strip():
        raise ValueError("registered source URL is empty")
    return source


def _validate_document_url(document_url: str, registered_url: str) -> None:
    try:
        document = urlsplit(document_url)
        registered = urlsplit(registered_url)
        document_port = document.port
        registered_port = registered.port
    except (TypeError, ValueError):
        raise ValueError("invalid official document URL") from None
    if (
        document.scheme != "https" or not document.hostname or document.username or document.password
        or document_port or document.fragment or registered.scheme != "https" or not registered.hostname
        or registered.username or registered.password or registered_port
    ):
        raise ValueError("official document URL must be a safe HTTPS URL")
    document_host = document.hostname.lower().removeprefix("www.")
    registered_host = registered.hostname.lower().removeprefix("www.")
    if document_host != registered_host and not document_host.endswith("." + registered_host):
        raise ValueError("official document host differs from registered source")


def _identity_matches(
    *,
    isin: str,
    issuer: str,
    issue_registration_number: str,
    expected_isin: str,
    expected_issuer: str,
    expected_issue_registration_number: str,
) -> bool:
    if expected_isin and isin.upper() != expected_isin.upper():
        return False
    if expected_issuer and _normal_name(issuer) != _normal_name(expected_issuer):
        return False
    if (
        expected_issue_registration_number
        and _normal_name(issue_registration_number) != _normal_name(expected_issue_registration_number)
    ):
        return False
    return True


def _normal_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
