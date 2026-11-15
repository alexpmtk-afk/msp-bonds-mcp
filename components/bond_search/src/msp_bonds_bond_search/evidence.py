"""Sequential evidence validation with fail-closed results and provenance."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
import json
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
    "rating": ("rating_grade", "rating_agency", "rating_scale", "rating_subject", "rating_action_date"),
    "coupon_terms": ("coupon_legal_terms", "coupon_schedule", "amortization_terms", "offer_status"),
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

    def __post_init__(self) -> None:
        if not self.claim.strip() or not self.source_name.strip():
            raise ValueError("evidence claim and source name are required")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        parts = urlsplit(self.source_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.port:
            raise ValueError("evidence URL must be a safe HTTPS URL")


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
            result = assess_stage(stage, items)
            results.append(result)
            blocked = result.state is not StageState.VERIFIED

        if results and all(item.state is StageState.VERIFIED for item in results):
            status = "VERIFIED_RESEARCH_RECORD_NOT_RECOMMENDATION"
        elif any(item.state is StageState.CONFLICT for item in results):
            status = "RESEARCH_RECORD_CONFLICT"
        else:
            status = "RESEARCH_RECORD_INCOMPLETE"
        return VerificationReport(isin=isin, stages=tuple(results), status=status)


def assess_stage(stage: str, evidence: Sequence[EvidenceItem]) -> StageResult:
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
    return StageResult(
        stage, StageState.VERIFIED, source_names=tuple(source_names),
        evidence_count=len(evidence), reason_code="ALL_REQUIRED_CLAIMS_CONFIRMED",
    )


def _stable_value(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)
