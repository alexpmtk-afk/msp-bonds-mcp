"""Validated, immutable snapshot of the two canonical Google Sheets tabs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any, Sequence


RULE_HEADERS = (
    "№", "Этап", "Правило / проверка", "Что фиксировать и как считать",
    "Где проверять", "Статус", "Примечание / основание",
)
RESOURCE_HEADERS = (
    "Приоритет", "Ресурс", "Ссылка", "Где и что смотреть",
    "Как использовать / сверять", "Статус", "Этапы логики",
)
_ACTIVE_STATUSES = {"ДЕЙСТВУЕТ", "АКТУАЛИЗИРОВАНО", "УТВЕРЖДЕНО"}


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
    """Original rows plus read time and a content fingerprint.

    Policy interpretation is handled by the local rule engine. Free-text rows
    are not treated as executable instructions merely because they are present.
    """

    rules: tuple[Rule, ...]
    resources: tuple[Resource, ...]
    fetched_at: datetime
    fingerprint: str

    @classmethod
    def from_sheet_rows(
        cls,
        rule_rows: Sequence[Sequence[Any]],
        resource_rows: Sequence[Sequence[Any]],
        *,
        fetched_at: datetime,
    ) -> "PolicySourceSnapshot":
        if fetched_at.tzinfo is None:
            raise SnapshotError("fetched_at must be timezone-aware")
        normalized_rules = _normalize_table(rule_rows, RULE_HEADERS, "rule")
        normalized_resources = _normalize_table(resource_rows, RESOURCE_HEADERS, "resource")
        try:
            rules = tuple(
                Rule(int(r[0]), *r[1:])
                for r in normalized_rules[1:]
                if any(cell.strip() for cell in r)
            )
        except (ValueError, TypeError) as exc:
            raise SnapshotError("invalid rule IDs or row schema") from exc
        sources = tuple(
            Resource(*r)
            for r in normalized_resources[1:]
            if any(cell.strip() for cell in r)
        )
        rule_ids = [r.rule_id for r in rules]
        if not rules or len(set(rule_ids)) != len(rule_ids):
            raise SnapshotError("rule IDs missing or repeated")
        if not sources or len({s.name for s in sources}) != len(sources):
            raise SnapshotError("source names missing or repeated")
        if any(not r.topic.strip() or not r.requirement.strip() or not r.status.strip() for r in rules):
            raise SnapshotError("incomplete rule")
        if any(not s.name.strip() or not s.purpose.strip() for s in sources):
            raise SnapshotError("incomplete resource")
        if not any(_status(r.status) in _ACTIVE_STATUSES for r in rules):
            raise SnapshotError("no explicitly active/approved rules in snapshot")

        raw = json.dumps(
            {"rules": normalized_rules, "resources": normalized_resources},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return cls(
            rules=rules,
            resources=sources,
            fetched_at=fetched_at.astimezone(timezone.utc),
            fingerprint=sha256(raw.encode("utf-8")).hexdigest(),
        )

    def assert_fresh(
        self,
        *,
        now: datetime | None = None,
        max_age: timedelta = timedelta(hours=1),
    ) -> None:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise SnapshotError("now must be timezone-aware")
        delta = now.astimezone(timezone.utc) - self.fetched_at
        if delta < timedelta(0) or delta > max_age:
            raise SnapshotError("canonical spreadsheet snapshot unavailable or stale")

    def rule(self, rule_id: int) -> Rule:
        matches = [rule for rule in self.rules if rule.rule_id == rule_id]
        if len(matches) != 1:
            raise SnapshotError(f"required rule unavailable: {rule_id}")
        return matches[0]

    def resource(self, name: str) -> Resource:
        matches = [source for source in self.resources if source.name == name]
        if len(matches) != 1:
            raise SnapshotError(f"required resource not configured: {name}")
        return matches[0]


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _normalize_table(
    rows: Sequence[Sequence[Any]],
    expected_header: Sequence[str],
    label: str,
) -> list[list[str]]:
    if not rows:
        raise SnapshotError(f"{label} tab is empty")
    normalized: list[list[str]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) > 7:
            raise SnapshotError(f"{label} tab must have exactly seven columns")
        cells = [_cell_text(value) for value in row]
        cells.extend([""] * (7 - len(cells)))
        normalized.append(cells)

    while len(normalized) > 1 and not any(cell.strip() for cell in normalized[-1]):
        normalized.pop()
    if tuple(normalized[0]) != tuple(expected_header):
        raise SnapshotError(f"unexpected {label} tab headers")
    for row in normalized[1:]:
        if any(cell.strip() for cell in row) and not row[0].strip():
            raise SnapshotError(f"{label} row is populated without an ID/name key")
    return normalized


def _status(value: str) -> str:
    return " ".join(value.upper().replace("Ё", "Е").split())
