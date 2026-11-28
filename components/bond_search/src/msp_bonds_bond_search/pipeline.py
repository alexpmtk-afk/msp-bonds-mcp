"""Research-only discovery with policy gates and bounded independent checks."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence
from urllib.parse import urlsplit
from dataclasses import replace

from .catalog import PolicySourceSnapshot, SnapshotError
from .discovery import (
    Candidate, SourceUnavailable, discover_moex, discover_smartlab,
    lookup_moex_isin, safe_get,
)
from .policy import SelectionPolicy


SearchAdapter = Callable[..., list[Candidate]]


@dataclass(frozen=True)
class DiscoveryReport:
    candidates: tuple[Candidate, ...]
    used_source: str | None
    source_errors: tuple[str, ...]
    policy_fingerprint: str
    discovered_at: datetime
    status: str = "RESEARCH_ONLY_NOT_VERIFIED"
    unimplemented_checks: tuple[str, ...] = (
        "issuer_identity_and_ruble_currency",
        "current_rating_and_rating_history",
        "legally_complete_coupon_and_redemption_calendar",
        "issuer_financial_condition_and_credit_events",
        "verified_cashflows_and_yield",
        "current_liquidity_and_portfolio_fit",
    )
    routed_only_sources: tuple[str, ...] = ("Bonds-Lab", "Cbonds", "Investmint", "Финам", "Investfunds")


class DiscoveryService:
    def __init__(
        self,
        *,
        snapshot: PolicySourceSnapshot,
        fetch: Callable[[str], bytes] = safe_get,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        additional_adapters: Sequence[tuple[str, SearchAdapter]] = (),
        moex_verification_limit: int = 5,
    ) -> None:
        if not 0 <= moex_verification_limit <= 20:
            raise ValueError("moex_verification_limit must be 0..20")
        self.snapshot = snapshot
        self.fetch = fetch
        self.now = now
        self.additional_adapters = tuple(additional_adapters)
        self.moex_verification_limit = moex_verification_limit

    def discover(self, *, task_authorized: bool = False, max_items: int = 50) -> DiscoveryReport:
        """Return unverified research leads only after a fresh approved policy load."""
        if not task_authorized:
            raise PermissionError("bond discovery requires an explicit user task")
        if not isinstance(max_items, int) or not 1 <= max_items <= 100:
            raise ValueError("max_items must be 1..100")
        timestamp = self.now()
        self.snapshot.assert_fresh(now=timestamp, max_age=timedelta(hours=1))
        SelectionPolicy.from_snapshot(self.snapshot)

        smart = self.snapshot.resource("Smart-Lab")
        moex = self.snapshot.resource("MOEX ISS")
        if urlsplit(smart.url).hostname not in ("smart-lab.ru", "www.smart-lab.ru"):
            raise SnapshotError("Smart-Lab registry URL differs from the expected host")
        if urlsplit(moex.url).hostname not in ("moex.com", "www.moex.com", "iss.moex.com"):
            raise SnapshotError("MOEX registry URL differs from the expected host")

        adapters: list[tuple[str, SearchAdapter]] = [
            ("Smart-Lab", discover_smartlab),
            ("MOEX ISS", discover_moex),
        ]
        seen = {name for name, _ in adapters}
        for name, adapter in self.additional_adapters:
            if name in seen:
                raise SnapshotError(f"duplicate discovery adapter: {name}")
            self.snapshot.resource(name)
            adapters.append((name, adapter))
            seen.add(name)

        errors: list[str] = []
        for label, adapter in adapters:
            try:
                rows = adapter(fetch=self.fetch, now=timestamp, max_items=max_items)
            except SourceUnavailable as exc:
                errors.append(f"{label}: {exc.diagnostic_summary}")
                continue
            except Exception as exc:
                # Do not expose provider payloads or request headers.
                errors.append(f"{label}: adapter error ({type(exc).__name__})")
                continue
            if not rows:
                errors.append(f"{label}: no candidates")
                continue

            candidates = list(rows)
            if label == "Smart-Lab" and self.moex_verification_limit:
                candidates = self._confirm_with_moex(candidates, timestamp, errors)
            return DiscoveryReport(
                candidates=tuple(candidates[:max_items]),
                used_source=label,
                source_errors=tuple(errors),
                policy_fingerprint=self.snapshot.fingerprint,
                discovered_at=timestamp,
            )
        return DiscoveryReport(
            candidates=(), used_source=None, source_errors=tuple(errors),
            policy_fingerprint=self.snapshot.fingerprint, discovered_at=timestamp,
            status="UNAVAILABLE_NO_RESULTS",
        )

    def _confirm_with_moex(
        self,
        candidates: list[Candidate],
        timestamp: datetime,
        errors: list[str],
    ) -> list[Candidate]:
        confirmed: dict[str, Candidate] = {candidate.isin: candidate for candidate in candidates}
        checks = candidates[: self.moex_verification_limit]
        for candidate in checks:
            try:
                result = lookup_moex_isin(
                    candidate.isin, fetch=self.fetch, now=timestamp,
                )
            except SourceUnavailable as exc:
                errors.append(f"MOEX ISS independent identifier check: {exc.diagnostic_summary}")
                continue
            if result.matched:
                current = confirmed[candidate.isin]
                confirmed[candidate.isin] = replace(
                    current,
                    independent_sources=tuple(dict.fromkeys((*current.independent_sources, "MOEX ISS"))),
                )
        return [confirmed[candidate.isin] for candidate in candidates]
