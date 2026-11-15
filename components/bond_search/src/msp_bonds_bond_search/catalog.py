"""Strict, read-only snapshot of the two canonical Google Sheets tabs.

The caller must use the existing authorized Google Workspace component to fetch
the tabs. This module does NOT implement Google auth or store spreadsheet IDs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Sequence


class SnapshotError(ValueError):
    """Required source/policy snapshot is absent, invalid or stale."""


@dataclass(frozen=True)
class Rule:
    rule_id: int
    topic: str
    requirement: str
    procedure: str
    reference: str
    status: str
    note: str


@dataclass(frozen=True)
class Resource:
    kind: str
    name: str
    url: str
    purpose: str
    procedure: str
    status: str
    rule_ids: str


@dataclass(frozen=True)
class PolicySourceSnapshot:
    """The complete source rows, NOT a hard-coded investment policy.

    Approval/priority semantics must remain with the canonical spreadsheet.
    Free-form rules are NOT automatically executable trading constraints.
    """

    rules: tuple[Rule, ...]
    resources: tuple[Resource, ...]
    fetched_at: datetime
    fingerprint: str

    @classmethod
    def from_sheet_rows(
        cls,
        rule_rows: Sequence[Sequence[str]],
        resource_rows: Sequence[Sequence[str]],
        *,
        fetched_at: datetime,
    ) -> "PolicySourceSnapshot":
        if fetched_at.tzinfo is None:
            raise SnapshotError("fetched_at must be timezone-aware")
        rule_rows = [list(map(str, row)) for row in rule_rows]
        resource_rows = [list(map(str, row)) for row in resource_rows]
        if not rule_rows or not resource_rows:
            raise SnapshotError("both canonical tabs are mandatory")
        if rule_rows[0][:2] != ["№", "Этап"]:
            raise SnapshotError("unexpected rule tab columns")
        if resource_rows[0][:2] != ["Приоритет", "Ресурс"]:
            raise SnapshotError("unexpected resource tab columns")
        if any(len(row) != 7 for row in rule_rows + resource_rows):
            raise SnapshotError("all rows must have exactly 7 columns")
        try:
            rules = tuple(Rule(int(r[0]), *r[1:]) for r in rule_rows[1:] if any(r))
        except (ValueError, TypeError) as exc:
            raise SnapshotError("invalid rule IDs or row schema") from exc
        sources = tuple(Resource(*r) for r in resource_rows[1:] if any(r))
        rule_ids = [r.rule_id for r in rules]
        if not rules or len(set(rule_ids)) != len(rule_ids):
            raise SnapshotError("rule IDs missing or repeated")
        if not sources or len({s.name for s in sources}) != len(sources):
            raise SnapshotError("source names missing or repeated")
        if any(not r.requirement.strip() or not r.status.strip() for r in rules):
            raise SnapshotError("incomplete rule")
        if any(not s.name.strip() or not s.purpose.strip() for s in sources):
            raise SnapshotError("incomplete resource")
        if not any("УТВЕРЖДЕНО" in r.status or "ДЕЙСТВУЕТ" in r.status for r in rules):
            raise SnapshotError("no active/approved rules; cannot proceed")
        raw = json.dumps(
            {"rules": rule_rows, "resources": resource_rows},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return cls(
            rules=rules,
            resources=sources,
            fetched_at=fetched_at.astimezone(timezone.utc),
            fingerprint=sha256(raw.encode("utf-8")).hexdigest(),
        )

    def assert_fresh(self, *, now: datetime | None = None, max_age: timedelta = timedelta(hours=1)) -> None:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise SnapshotError("now must be timezone-aware")
        delta = now.astimezone(timezone.utc) - self.fetched_at
        if delta < timedelta(0) or delta > max_age:
            raise SnapshotError("canonical spreadsheet snapshot unavailable or stale")

    def resource(self, name: str) -> Resource:
        matches = [source for source in self.resources if source.name == name]
        if len(matches) != 1:
            raise SnapshotError(f"required resource not configured: {name}")
        return matches[0]
