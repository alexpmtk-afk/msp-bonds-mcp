"""Sequential evidence validation with fail-closed results and provenance."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
import json
import re
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit


class EvidenceState(str, Enum):
    CONFIRMED = "CONFIRMED"
    UNVERIFIED = "UNVERIFIED"
    MISSING = "MISSING"
    CONFLICT = "CONFLICT"


class EvidenceKind(str, Enum):
    PRIMARY = "PRIMARY"
    OFFICIAL = "OFFICIAL"
    SECONDARY = "SECONDARY"
    CALCULATION = "CALCULATION"


class StageState(str, Enum):
    VERIFIED = "VERIFIED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CONFLICT = "CONFLICT"
    NOT_RUN = "NOT_RUN"


STAGE_ORDER: tuple[str, ...] = (
    "identity",
    "rating",
    "coupon_terms",
    "yield",
    "financial_condition",
    "bankruptcy",
    "default_events",
    "liquidity",
)

REQUIRED_CLAIMS: Mapping[str, tuple[str, ...]] = {
    "identity": ("isin", "issuer", "issuer_country", "instrument_type", "currency", "maturity_date"),
    "rating": (
        "rating_grade", "rating_agency", "rating_scale", "rating_subject",
        "rating_subject_type", "rating_subject_id", "rating_action_date",
        "rating_action_type", "rating_status", "rating_action_is_latest",
    ),
    "coupon_terms": (
        "coupon_legal_terms", "coupon_schedule", "amortization_terms", "offer_status",
        "future_coupon_terms_legally_defined", "issue_document_is_current",
    ),
    "yield": ("settlement_date", "full_entry_cost", "verified_cashflows", "xirr"),
    "financial_condition": ("financial_period", "financial_statements", "debt_metrics", "cash_flow_metrics"),
    "bankruptcy": ("bankruptcy_search_result", "insolvency_status"),
    "default_events": ("default_search_result", "overdue_status", "restructuring_status", "disclosure_events"),
    "liquidity": ("market_observed_at", "best_bid", "best_ask", "bid_size", "ask_size", "turnover"),
}

_ALLOWED_KINDS: Mapping[str, frozenset[EvidenceKind]] = {
    "identity": frozenset({EvidenceKind.PRIMARY, EvidenceKind.OFFICIAL}),
    "rating": frozenset({EvidenceKind.PRIMARY, EvidenceKind.OFFICIAL}),
    "coupon_terms": frozenset({EvidenceKind.PRIMARY}),
    "yield": frozenset({EvidenceKind.PRIMARY, EvidenceKind.CALCULATION}),
    "financial_condition": frozenset({EvidenceKind.PRIMARY, EvidenceKind.OFFICIAL}),
    "bankruptcy": frozenset({EvidenceKind.PRIMARY, EvidenceKind.OFFICIAL}),
    "default_events": frozenset({EvidenceKind.PRIMARY, EvidenceKind.OFFICIAL}),
    "liquidity": frozenset({EvidenceKind.PRIMARY, EvidenceKind.OFFICIAL}),
}


@dataclass(frozen=True)
class DocumentProvenance:
    """Integrity and identity metadata for a supplied primary document."""

    document_type: str
    document_id: str
    issuer: str
    isin: str
    issue_registration_number: str
    document_date: date
    source_name: str
    source_url: str
    retrieved_at: datetime
    artifact_sha256: str
    locator: str = ""
    source_status: str = "UNKNOWN"

    def __post_init__(self) -> None:
        string_fields = (
            self.document_type, self.document_id, self.issuer, self.isin,
            self.issue_registration_number, self.source_name, self.source_url,
            self.artifact_sha256, self.locator, self.source_status,
        )
        if not all(isinstance(value, str) for value in string_fields):
            raise ValueError("document provenance text fields must be strings")
        if not self.document_type.strip() or not self.issuer.strip() or not self.source_name.strip():
            raise ValueError("document type, issuer and source name are required")
        if not (self.document_id.strip() or self.isin.strip() or self.issue_registration_number.strip()):
            raise ValueError("document or issue identifier is required")
        if self.isin and not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", self.isin.upper()):
            raise ValueError("document ISIN has an invalid shape")
        if not isinstance(self.document_date, date):
            raise ValueError("document date is required")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("document retrieval time must be timezone-aware")
        if self.document_date > self.retrieved_at.date():
            raise ValueError("document date cannot be in the future")
        if not re.fullmatch(r"[0-9a-f]{64}", self.artifact_sha256):
            raise ValueError("document SHA-256 must be 64 lowercase hexadecimal characters")
        parts = urlsplit(self.source_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.port:
            raise ValueError("document source must be a safe HTTPS URL")
        if self.source_status not in {"CURRENT", "UNKNOWN", "SUPERSEDED", "WITHDRAWN", "UNDER_REVIEW"}:
            raise ValueError("unsupported document currentness status")


@dataclass(frozen=True)
class EvidenceItem:
    claim: str
    value: object = field(repr=False)
    unit: str
    source_name: str
    source_url: str = field(repr=False)
    kind: EvidenceKind
    state: EvidenceState
    retrieved_at: datetime
    document_date: date | None = None
    method: str = ""
    note: str = field(default="", repr=False)
    document_provenance: DocumentProvenance | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.claim.strip() or not self.source_name.strip():
            raise ValueError("evidence claim and source name are required")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        parts = urlsplit(self.source_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.port:
            raise ValueError("evidence URL must be a safe HTTPS URL")
        if self.document_provenance is not None:
            provenance = self.document_provenance
            if provenance.source_name != self.source_name or provenance.source_url != self.source_url:
                raise ValueError("evidence source differs from document provenance")
            if provenance.document_date != self.document_date or provenance.retrieved_at != self.retrieved_at:
                raise ValueError("evidence dates differ from document provenance")


@dataclass(frozen=True)
class StageResult:
    stage: str
    state: StageState
    missing_claims: tuple[str, ...] = ()
    source_names: tuple[str, ...] = ()
    evidence_count: int = 0
    reason_code: str = ""
    conflicting_claims: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationReport:
    isin: str
    stages: tuple[StageResult, ...]
    status: str
    recommendation: str = "NONE"

    @property
    def complete(self) -> bool:
        return self.status == "VERIFIED_RESEARCH_RECORD_NOT_RECOMMENDATION"


EvidenceProvider = Callable[[str], Iterable[EvidenceItem]]


class SequentialEvidenceVerifier:
    """Runs provider stages in order and stops when a prerequisite is incomplete."""

    def verify(
        self,
        isin: str,
        providers: Mapping[str, EvidenceProvider],
        *,
        expected_issuer: str | None = None,
    ) -> VerificationReport:
        if not isin or len(isin) != 12 or not isin.startswith("RU"):
            raise ValueError("a Russian-format ISIN lead is required")
        results: list[StageResult] = []
        blocked = False
        for stage in STAGE_ORDER:
            if blocked:
                results.append(StageResult(stage, StageState.NOT_RUN, reason_code="PREVIOUS_STAGE_INCOMPLETE"))
                continue
            provider = providers.get(stage)
            if provider is None:
                results.append(StageResult(stage, StageState.NEEDS_REVIEW, reason_code="PROVIDER_NOT_IMPLEMENTED"))
                blocked = True
                continue
            try:
                items = tuple(provider(isin))
            except Exception as exc:
                results.append(StageResult(stage, StageState.NEEDS_REVIEW, reason_code=f"PROVIDER_ERROR_{type(exc).__name__}"))
                blocked = True
                continue
            result = assess_stage(
                stage, items, expected_isin=isin, expected_issuer=expected_issuer,
            )
            results.append(result)
            blocked = result.state is not StageState.VERIFIED

        if results and all(item.state is StageState.VERIFIED for item in results):
            status = "VERIFIED_RESEARCH_RECORD_NOT_RECOMMENDATION"
        elif any(item.state is StageState.CONFLICT for item in results):
            status = "RESEARCH_RECORD_CONFLICT"
        else:
            status = "RESEARCH_RECORD_INCOMPLETE"
        return VerificationReport(isin=isin, stages=tuple(results), status=status)


def assess_stage(
    stage: str,
    evidence: Sequence[EvidenceItem],
    *,
    expected_isin: str | None = None,
    expected_issuer: str | None = None,
) -> StageResult:
    if stage not in REQUIRED_CLAIMS:
        raise ValueError(f"unknown verification stage: {stage}")
    required = REQUIRED_CLAIMS[stage]
    confirmed: dict[str, list[EvidenceItem]] = {}
    explicit_conflicts: set[str] = set()
    source_names: list[str] = []
    for item in evidence:
        if item.claim not in required:
            continue
        if item.source_name not in source_names:
            source_names.append(item.source_name)
        if item.kind not in _ALLOWED_KINDS[stage]:
            continue
        provenance = item.document_provenance
        if provenance is not None and provenance.source_status != "CURRENT":
            # A superseded, withdrawn, under-review, or unclassified document
            # cannot establish current terms or a current rating action.
            continue
        if stage in {"rating", "coupon_terms"} and item.state is EvidenceState.CONFIRMED and provenance is None:
            # These high-impact claims require a traceable official artifact.
            continue
        if item.state is EvidenceState.CONFLICT:
            explicit_conflicts.add(item.claim)
        elif (
            item.state is EvidenceState.CONFIRMED
            and item.value is not None
            and item.value != ""
        ):
            confirmed.setdefault(item.claim, []).append(item)

    disagreements = set()
    for claim, items in confirmed.items():
        values = {(item.unit, _stable_value(item.value)) for item in items}
        if len(values) > 1:
            disagreements.add(claim)
    conflicts = tuple(sorted(explicit_conflicts | disagreements))
    if conflicts:
        return StageResult(
            stage, StageState.CONFLICT,
            source_names=tuple(source_names), evidence_count=len(evidence),
            reason_code=(
                "EXPLICIT_CONFLICT_EVIDENCE" if explicit_conflicts
                else "CONFIRMED_SOURCES_DISAGREE"
            ),
            conflicting_claims=conflicts,
        )

    # MISSING means this source did not supply the claim; it is not a contrary
    # value and cannot veto a separate valid confirmation. A required claim
    # still remains open if no eligible source confirms it.
    missing = tuple(claim for claim in required if claim not in confirmed)
    if missing:
        return StageResult(
            stage, StageState.NEEDS_REVIEW, missing_claims=missing,
            source_names=tuple(source_names), evidence_count=len(evidence),
            reason_code="REQUIRED_PRIMARY_EVIDENCE_MISSING",
        )

    values = {claim: items[0].value for claim, items in confirmed.items()}
    if stage == "rating":
        subject_type = _normal_token(values["rating_subject_type"])
        subject_id = " ".join(str(values["rating_subject_id"]).split()).casefold()
        if subject_type == "ISSUE":
            subject_matches = expected_isin is not None and subject_id == expected_isin.casefold()
        elif subject_type == "ISSUER":
            subject_matches = expected_issuer is not None and subject_id == " ".join(expected_issuer.split()).casefold()
        else:
            subject_matches = False
        if not subject_matches:
            return StageResult(
                stage, StageState.NEEDS_REVIEW,
                missing_claims=("rating_subject_match",),
                source_names=tuple(source_names), evidence_count=len(evidence),
                reason_code="RATING_SUBJECT_MISMATCH_OR_UNKNOWN",
            )
        if normalize_rating_status(values["rating_status"]) != "ACTIVE":
            return StageResult(
                stage, StageState.NEEDS_REVIEW,
                missing_claims=("current_rating_status",),
                source_names=tuple(source_names), evidence_count=len(evidence),
                reason_code="RATING_NOT_CONFIRMED_ACTIVE",
            )
        if values["rating_action_is_latest"] is not True:
            return StageResult(
                stage, StageState.NEEDS_REVIEW,
                missing_claims=("latest_rating_action",),
                source_names=tuple(source_names), evidence_count=len(evidence),
                reason_code="LATEST_RATING_ACTION_NOT_CONFIRMED",
            )
    elif stage == "coupon_terms":
        if not isinstance(values["future_coupon_terms_legally_defined"], bool):
            return StageResult(
                stage, StageState.NEEDS_REVIEW,
                missing_claims=("future_coupon_terms_legally_defined",),
                source_names=tuple(source_names), evidence_count=len(evidence),
                reason_code="FUTURE_COUPON_TERMS_UNKNOWN",
            )
        if values["issue_document_is_current"] is not True:
            return StageResult(
                stage, StageState.NEEDS_REVIEW,
                missing_claims=("current_issue_document",),
                source_names=tuple(source_names), evidence_count=len(evidence),
                reason_code="ISSUE_DOCUMENT_NOT_CONFIRMED_CURRENT",
            )
    return StageResult(
        stage, StageState.VERIFIED, source_names=tuple(source_names),
        evidence_count=len(evidence), reason_code="ALL_REQUIRED_CLAIMS_CONFIRMED",
    )


def _stable_value(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)


def _normal_token(value: object) -> str:
    return "_".join(str(value).upper().replace("Ё", "Е").split())


def normalize_rating_status(value: object) -> str:
    """Map only explicit common official labels; unknown labels stay unknown."""
    token = _normal_token(value)
    if token in {"ACTIVE", "CURRENT", "ДЕЙСТВУЕТ", "U", "ORDINARY", "ОБЫЧНЫЙ"}:
        return "ACTIVE"
    if token in {"W", "UNDER_REVIEW", "WATCH", "ON_REVIEW", "ПОД_НАБЛЮДЕНИЕМ", "НА_ПЕРЕСМОТРЕ"}:
        return "UNDER_REVIEW"
    if token in {"WD", "WITHDRAWN", "REVOKED", "ОТОЗВАН", "ОТЗЫВ"}:
        return "WITHDRAWN"
    return "UNKNOWN"
