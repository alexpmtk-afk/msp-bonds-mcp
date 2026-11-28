"""Evidence routing by information need. Routes do not verify source contents."""
from __future__ import annotations

from dataclasses import dataclass

from .catalog import PolicySourceSnapshot, Resource, SnapshotError


EVIDENCE_ROUTES: dict[str, tuple[str, ...]] = {
    "discovery": ("Smart-Lab", "MOEX ISS", "Bonds-Lab", "Cbonds", "Investmint", "Финам", "Investfunds"),
    "identity": ("MOEX ISS", "Московская биржа — облигации", "ФНС — Прозрачный бизнес"),
    "market_price": ("MOEX ISS", "Московская биржа — облигации", "ВТБ — стакан и доступность бумаги"),
    "coupon_terms": ("НРД / NSD Data", "Интерфакс — раскрытие", "Официальный сайт эмитента"),
    "rating": ("Банк России — рейтинговый репозитарий", "АКРА", "Эксперт РА", "НКР", "НРА"),
    "yield": ("MOEX ISS", "Московская биржа — облигации"),
    "financial_condition": ("ФНС — БФО", "Официальный сайт эмитента", "Интерфакс — раскрытие"),
    "bankruptcy": ("Федресурс", "Картотека арбитражных дел", "ФНС — Прозрачный бизнес"),
    "default_events": ("Федресурс", "Интерфакс — раскрытие", "Картотека арбитражных дел", "НРД / NSD Data"),
    "liquidity": ("MOEX ISS", "Московская биржа — облигации", "ВТБ — стакан и доступность бумаги"),
    "portfolio": ("Единая таблица «Портфель облигаций»", "Архив брокерских отчётов ВТБ"),
}

IMPLEMENTED_DISCOVERY_ADAPTERS = frozenset({"Smart-Lab", "MOEX ISS"})


@dataclass(frozen=True)
class EvidenceRoute:
    field: str
    providers: tuple[Resource, ...]
    missing_from_registry: tuple[str, ...]
    available_adapters: tuple[str, ...]
    routed_only: tuple[str, ...]
    status: str = "ROUTE_ONLY_NOT_VERIFIED"


def plan_evidence_route(snapshot: PolicySourceSnapshot, field: str) -> EvidenceRoute:
    """Choose configured sources in a deterministic order and label adapter gaps."""
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
    available = tuple(source.name for source in providers if source.name in IMPLEMENTED_DISCOVERY_ADAPTERS and field == "discovery")
    routed_only = tuple(source.name for source in providers if source.name not in available)
    return EvidenceRoute(field, tuple(providers), tuple(missing), available, routed_only)
