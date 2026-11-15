"""Evidence routing by information need. Plans only; no provider is auto-trusted."""
from __future__ import annotations

from dataclasses import dataclass

from .catalog import PolicySourceSnapshot, Resource, SnapshotError


# Public provider responsibilities only; private rating thresholds and portfolio
# constraints are intentionally absent from the repository.
EVIDENCE_ROUTES: dict[str, tuple[str, ...]] = {
    "discovery": ("Smart-Lab", "MOEX ISS", "Bonds-Lab", "Cbonds"),
    "market_price": ("MOEX ISS", "Московская биржа — облигации", "ВТБ — стакан и доступность бумаги"),
    "coupon_terms": ("НРД / NSD Data", "Интерфакс — раскрытие", "Официальный сайт эмитента"),
    "rating": (
        "Банк России — рейтинговый репозитарий", "АКРА",
        "Эксперт РА", "НКР", "НРА",
    ),
    "financial_health": ("ФНС — БФО", "Официальный сайт эмитента", "Интерфакс — раскрытие"),
    "default_events": ("Федресурс", "Интерфакс — раскрытие", "Картотека арбитражных дел"),
    "legal_identity": ("ФНС — Прозрачный бизнес", "Интерфакс — раскрытие"),
    "portfolio": ("Единая таблица «Портфель облигаций»", "Архив брокерских отчётов ВТБ"),
}


@dataclass(frozen=True)
class EvidenceRoute:
    field: str
    providers: tuple[Resource, ...]
    missing_from_registry: tuple[str, ...]
    status: str = "ROUTE_ONLY_NOT_VERIFIED"


def plan_evidence_route(snapshot: PolicySourceSnapshot, field: str) -> EvidenceRoute:
    """Choose configured sources. An available URL != a verified data point.

    Providers must implement separate read-only adapters with source-specific
    schema, provenance and checks. No fallback supplies invented data.
    """
    if field not in EVIDENCE_ROUTES:
        raise ValueError(f"unknown evidence need: {field}")
    providers: list[Resource] = []
    missing: list[str] = []
    for name in EVIDENCE_ROUTES[field]:
        try:
            source = snapshot.resource(name)
        except SnapshotError:
            missing.append(name)
        else:
            providers.append(source)
    if not providers:
        raise SnapshotError(f"no registered source for evidence field: {field}")
    return EvidenceRoute(field, tuple(providers), tuple(missing))
