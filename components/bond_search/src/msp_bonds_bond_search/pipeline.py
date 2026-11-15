"""Research-only discovery coordinator with explicit per-source failover.

No issuer can be recommended by this module. Ratings, cashflows, defaults,
liquidity, portfolio concentration and executable quotes require future gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlsplit

from .catalog import PolicySourceSnapshot, SnapshotError
from .discovery import (
    Candidate, SourceUnavailable, discover_moex, discover_smartlab, safe_get,
)


@dataclass(frozen=True)
class DiscoveryReport:
    candidates: tuple[Candidate, ...]
    used_source: str | None
    source_errors: tuple[str, ...]
    policy_fingerprint: str
    discovered_at: datetime
    status: str = "RESEARCH_ONLY_NOT_VERIFIED"
    unimplemented_checks: tuple[str, ...] = (
        "corporate_issuer_and_ruble_currency",
        "rating_and_rating_history",
        "legally_complete_coupon_and_redemption_calendar",
        "issuer_financial_condition_and_default",
        "reliable_yield_and_executable_price",
        "liquidity_and_portfolio_concentration",
    )


class DiscoveryService:
    def __init__(
        self,
        *,
        snapshot: PolicySourceSnapshot,
        fetch: Callable[[str], bytes] = safe_get,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.snapshot = snapshot
        self.fetch = fetch
        self.now = now

    def discover(self, *, task_authorized: bool = False, max_items: int = 50) -> DiscoveryReport:
        """Only for a fresh, explicitly user-requested research task.

        Returned ISINs are leads, NOT confirmed corporate bonds or advice.
        The MCP host must independently authenticate/authorize its real caller.
        """
        if not task_authorized:
            raise PermissionError("bond discovery requires an explicit user task")
        if not isinstance(max_items, int) or not 1 <= max_items <= 100:
            raise ValueError("max_items must be 1..100")
        timestamp = self.now()
        self.snapshot.assert_fresh(now=timestamp, max_age=timedelta(hours=1))
        # Source map is defined in the user's live worksheet, not rewritten here.
        smart = self.snapshot.resource("Smart-Lab")
        moex = self.snapshot.resource("MOEX ISS")
        if urlsplit(smart.url).hostname not in ("smart-lab.ru", "www.smart-lab.ru"):
            raise SnapshotError("Smart-Lab source URL differs from expected host")
        if urlsplit(moex.url).hostname not in ("moex.com", "www.moex.com", "iss.moex.com"):
            raise SnapshotError("MOEX source URL differs from expected host")

        errors: list[str] = []
        for label, adapter in (("Smart-Lab", discover_smartlab), ("MOEX ISS", discover_moex)):
            try:
                rows = adapter(fetch=self.fetch, now=timestamp, max_items=max_items)
            except SourceUnavailable as exc:
                errors.append(f"{label}: {exc}")
                continue
            if rows:
                return DiscoveryReport(
                    candidates=tuple(rows), used_source=label,
                    source_errors=tuple(errors),
                    policy_fingerprint=self.snapshot.fingerprint,
                    discovered_at=timestamp,
                )
            errors.append(f"{label}: no candidates")
        return DiscoveryReport(
            candidates=(), used_source=None, source_errors=tuple(errors),
            policy_fingerprint=self.snapshot.fingerprint, discovered_at=timestamp,
            status="UNAVAILABLE_NO_RESULTS",
        )
