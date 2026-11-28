"""Interpret only the approved subset of the canonical policy sheet.

Historical methods and unapproved thresholds remain visible in the snapshot but
never become enforceable constraints by text similarity alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
import re

from .catalog import PolicySourceSnapshot, Rule, SnapshotError


class PolicyConfigurationError(SnapshotError):
    """The canonical policy changed in a way this interpreter cannot apply safely."""


class RuleDisposition(str, Enum):
    BINDING = "BINDING"
    PREFERENCE = "PREFERENCE"
    WORKING_INTERPRETATION = "WORKING_INTERPRETATION"
    PRINCIPLE_ONLY = "PRINCIPLE_ONLY"
    NON_BINDING = "NON_BINDING"


class ScreenStatus(str, Enum):
    PASS_RESEARCH_ONLY = "PASS_RESEARCH_ONLY"
    NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
    REJECTED_BY_POLICY = "REJECTED_BY_POLICY"


def normalize_policy_text(value: str) -> str:
    value = value.upper().replace("Ё", "Е").replace("−", "-").replace("–", "-").replace("—", "-")
    return " ".join(value.split())


def rule_disposition(status: str) -> RuleDisposition:
    value = normalize_policy_text(status)
    if value in {"ДЕЙСТВУЕТ", "АКТУАЛИЗИРОВАНО", "УТВЕРЖДЕНО"}:
        return RuleDisposition.BINDING
    if value == "УТВЕРЖДЕНО - ПРЕДПОЧТЕНИЕ":
        return RuleDisposition.PREFERENCE
    if value == "УТВЕРЖДЕНО (BBB); BBB+ - ТРАКТОВКА":
        return RuleDisposition.WORKING_INTERPRETATION
    if value in {
        "ПЛАН / ПРИНЦИП УТВЕРЖДЕН",
        "ПРИНЦИП УТВЕРЖДЕН / ПОРОГ НЕ УТВЕРЖДЕН",
        "ПРИНЦИП УТВЕРЖДЕН / МЕТОДИКА В РАЗРАБОТКЕ",
    }:
        return RuleDisposition.PRINCIPLE_ONLY
    return RuleDisposition.NON_BINDING


_REQUIRED_RULES: dict[int, tuple[str, RuleDisposition]] = {
    1: ("Область поиска", RuleDisposition.BINDING),
    2: ("Начало анализа", RuleDisposition.BINDING),
    23: ("Действующие ограничения", RuleDisposition.BINDING),
    24: ("Достоверность", RuleDisposition.BINDING),
    25: ("Ограничение действий", RuleDisposition.BINDING),
    58: ("ПОЛИТИКА ОТБОРА - 15.11.2026", RuleDisposition.BINDING),
    59: ("Валюта", RuleDisposition.BINDING),
    60: ("Номинал", RuleDisposition.BINDING),
    61: ("Минимальный кредитный рейтинг", RuleDisposition.BINDING),
    62: ("Приоритет качества", RuleDisposition.BINDING),
    63: ("Ограничение количества BBB", RuleDisposition.WORKING_INTERPRETATION),
    64: ("Углубленная проверка BBB", RuleDisposition.BINDING),
    65: ("Плавающий купон", RuleDisposition.BINDING),
    66: ("Неизвестные будущие купоны", RuleDisposition.BINDING),
    67: ("Доходность и рынок", RuleDisposition.BINDING),
    68: ("Срок погашения", RuleDisposition.PREFERENCE),
    69: ("Концентрация по эмитентам и группам", RuleDisposition.PRINCIPLE_ONLY),
    70: ("Ликвидность", RuleDisposition.PRINCIPLE_ONLY),
    71: ("Глубина проверки", RuleDisposition.PRINCIPLE_ONLY),
}


def _normalized_topic(value: str) -> str:
    return normalize_policy_text(value).replace("Ё", "Е")


def _contains_all(text: str, fragments: tuple[str, ...]) -> bool:
    normalized = normalize_policy_text(text)
    return all(normalize_policy_text(fragment) in normalized for fragment in fragments)


@dataclass(frozen=True)
class CandidateFacts:
    issuer_country: str | None = None
    instrument_type: str | None = None
    currency: str | None = None
    rating_grade: str | None = None
    rating_confirmed: bool = False
    rating_agency: str | None = None
    rating_scale: str | None = None
    rating_subject_matches: bool = False
    rating_action_date: date | None = None
    floating_coupon: bool | None = None
    floating_formula_officially_confirmed: bool | None = None
    future_coupon_terms_legally_defined: bool | None = None
    maturity_date: date | None = None
    as_of: date | None = None
    existing_bbb_units_by_issuer: int | None = None
    proposed_units: int | None = None


@dataclass(frozen=True)
class PolicyAssessment:
    status: ScreenStatus
    reasons: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    preference_flags: tuple[str, ...]
    policy_fingerprint: str
    portfolio_suitability: str = "UNVERIFIED_NO_APPROVED_PERCENT_RISK_MODEL"
    recommendation: str = "NONE"


@dataclass(frozen=True)
class SelectionPolicy:
    """Executable values derived from specifically approved sheet rows."""

    snapshot: PolicySourceSnapshot
    minimum_rating: str
    bbb_units_limit: int
    preferred_maturity_years: int
    minimum_yield_percent: None
    bbb_plus_is_working_interpretation: bool = True
    percentage_concentration_limit: None = None
    nominal_constraint: None = None

    @classmethod
    def from_snapshot(cls, snapshot: PolicySourceSnapshot) -> "SelectionPolicy":
        by_id = {rule.rule_id: rule for rule in snapshot.rules}
        missing = sorted(set(_REQUIRED_RULES) - set(by_id))
        if missing:
            raise PolicyConfigurationError(f"required approved policy rows missing: {missing}")

        for rule_id, (topic, expected_disposition) in _REQUIRED_RULES.items():
            rule = by_id[rule_id]
            if _normalized_topic(rule.topic) != _normalized_topic(topic):
                raise PolicyConfigurationError(f"policy row {rule_id} topic changed; interpreter review required")
            actual = rule_disposition(rule.status)
            if actual is not expected_disposition:
                raise PolicyConfigurationError(f"policy row {rule_id} status changed; interpreter review required")

        text = lambda rule_id: " ".join((
            by_id[rule_id].requirement, by_id[rule_id].procedure, by_id[rule_id].note,
        ))
        checks = {
            1: ("РОССИЙСКИХ ЭМИТЕНТОВ", "КОРПОРАТИВНЫЕ ОБЛИГАЦИИ"),
            2: ("ОТДЕЛЬНОГО ПОРУЧЕНИЯ",),
            23: ("A-", "BBB", "5"),
            24: ("ПЕРВОИСТОЧНИК", "СТАТУС"),
            25: ("НЕ ТОРГОВАТЬ", "НЕ УПРАВЛЯТЬ СЧЕТОМ"),
            58: ("ОТДЕЛЬНОМУ ЗАДАНИЮ",),
            59: ("РУБЛЕВЫЕ", "РОССИЙСКИХ ЭМИТЕНТОВ"),
            60: ("НЕТ ОГРАНИЧЕНИЯ ПО НОМИНАЛУ",),
            61: ("МИНИМАЛЬНО ДОПУСТИМЫЙ", "BBB"),
            62: ("A-", "НЕ ОГРАНИЧИВАЕТСЯ ПЯТЬЮ"),
            63: ("НЕ БОЛЕЕ 5", "BBB+", "РАБОЧЕЙ ТРАКТОВКЕ"),
            64: ("BBB", "ТЩАТЕЛЬНОЙ ПРОВЕРКОЙ"),
            65: ("ФЛОАТЕРЫ ДОПУСКАЮТСЯ", "ОФИЦИАЛЬНО ПОДТВЕРЖДЕНА"),
            66: ("ЮРИДИЧЕСКИ НЕ ОПРЕДЕЛЕНЫ",),
            67: ("НЕ УСТАНАВЛИВАТЬ ПОСТОЯННЫЙ МИНИМАЛЬНЫЙ",),
            68: ("ПРЕДПОЧТИТЕЛЬНЫЙ СРОК", "НЕ БОЛЕЕ 3 ЛЕТ", "НЕ АВТОМАТИЧЕСКИЙ ЗАПРЕТ"),
            69: ("ПРЕДСТОИТ РАЗРАБОТАТЬ", "НЕ СЧИТАТЬ УТВЕРЖДЕННЫМИ"),
            70: ("ЛИКВИДНОСТЬ УЧИТЫВАТЬ ОБЯЗАТЕЛЬНО", "НЕ УСТАНОВИЛ"),
            71: ("ДЕТАЛЬНОСТЬ ПРОВЕРКИ ОПРЕДЕЛЯТЬ", "НЕ УТВЕРЖДЕНА"),
        }
        for rule_id, fragments in checks.items():
            if not _contains_all(text(rule_id), fragments):
                raise PolicyConfigurationError(f"policy row {rule_id} text changed; interpreter review required")

        floor_match = re.search(r"МИНИМАЛЬНО ДОПУСТИМЫЙ.*?-\s*(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC[+-]?)\b", normalize_policy_text(text(61)))
        if not floor_match:
            raise PolicyConfigurationError("minimum rating cannot be parsed safely")
        count_match = re.search(r"НЕ БОЛЕЕ\s+(\d+)\s+ОБЛИГАЦ", normalize_policy_text(text(63)))
        if not count_match:
            raise PolicyConfigurationError("BBB quantity cap cannot be parsed safely")
        years_match = re.search(r"НЕ БОЛЕЕ\s+(\d+)\s+ЛЕТ", normalize_policy_text(text(68)))
        if not years_match:
            raise PolicyConfigurationError("maturity preference cannot be parsed safely")

        # Rows 67 and 69 explicitly do not approve fixed yield or percentage
        # concentration thresholds. Do not infer or invent either value.
        return cls(
            snapshot=snapshot,
            minimum_rating=floor_match.group(1),
            bbb_units_limit=int(count_match.group(1)),
            preferred_maturity_years=int(years_match.group(1)),
            minimum_yield_percent=None,
            percentage_concentration_limit=None,
        )

    def disposition(self, rule_id: int) -> RuleDisposition:
        # The engine applies only explicitly reviewed identifiers. Even if an
        # old row is later relabelled approved, it is not executable here.
        if rule_id not in _REQUIRED_RULES:
            return RuleDisposition.NON_BINDING
        rule = next((r for r in self.snapshot.rules if r.rule_id == rule_id), None)
        return RuleDisposition.NON_BINDING if rule is None else rule_disposition(rule.status)

    def evaluate(self, facts: CandidateFacts) -> PolicyAssessment:
        rejects: list[str] = []
        missing: list[str] = []
        preferences: list[str] = []

        if facts.issuer_country is None:
            missing.append("issuer_country")
        elif facts.issuer_country.strip().upper() not in {"RU", "RUS", "RUSSIA", "РОССИЯ"}:
            rejects.append("issuer_not_russian")

        if facts.instrument_type is None:
            missing.append("instrument_type")
        elif facts.instrument_type.strip().upper() not in {"CORPORATE_BOND", "КОРПОРАТИВНАЯ ОБЛИГАЦИЯ"}:
            rejects.append("instrument_not_corporate_bond")

        if facts.currency is None:
            missing.append("currency")
        elif facts.currency.strip().upper() not in {"RUB", "RUR", "РУБ", "РУБ."}:
            rejects.append("currency_not_rub")

        if not facts.rating_confirmed:
            missing.append("current_rating_confirmation")
        if not facts.rating_agency:
            missing.append("rating_agency")
        if not facts.rating_scale:
            missing.append("rating_scale")
        if not facts.rating_subject_matches:
            missing.append("rating_subject_match")
        if not facts.rating_action_date:
            missing.append("rating_action_date")

        grade = _canonical_grade(facts.rating_grade or "")
        if facts.rating_confirmed and grade is None:
            missing.append("normalized_rating_grade")
        if facts.rating_confirmed and grade is not None:
            order = _rating_order()
            floor = _canonical_grade(self.minimum_rating)
            if floor is None:
                raise PolicyConfigurationError("unsupported minimum rating in policy")
            if order.index(grade) > order.index(floor):
                rejects.append("rating_below_approved_minimum")
            elif order.index(grade) >= order.index("A"):
                preferences.append("enhanced_credit_review_required_below_A")

            if grade == "BBB":
                if facts.existing_bbb_units_by_issuer is None or facts.proposed_units is None:
                    missing.append("bbb_issuer_units_and_proposed_units")
                elif facts.existing_bbb_units_by_issuer + facts.proposed_units > self.bbb_units_limit:
                    rejects.append("approved_bbb_issuer_unit_limit_exceeded")
            elif grade == "BBB+":
                preferences.append("BBB+ cap is a working interpretation, not a fully approved hard rule")
                if facts.existing_bbb_units_by_issuer is None or facts.proposed_units is None:
                    missing.append("bbbplus_issuer_units_for_working_interpretation")
                elif facts.existing_bbb_units_by_issuer + facts.proposed_units > self.bbb_units_limit:
                    preferences.append("BBB+ working interpretation cap would be exceeded; owner review required")

        if facts.future_coupon_terms_legally_defined is None:
            missing.append("future_coupon_terms_legally_defined")
        elif not facts.future_coupon_terms_legally_defined:
            rejects.append("future_coupon_terms_not_legally_defined")

        if facts.floating_coupon is None:
            missing.append("floating_coupon_classification")
        elif facts.floating_coupon:
            if facts.floating_formula_officially_confirmed is None:
                missing.append("official_floating_coupon_formula")
            elif not facts.floating_formula_officially_confirmed:
                rejects.append("floating_coupon_formula_not_officially_confirmed")

        if facts.maturity_date is None or facts.as_of is None:
            missing.append("maturity_date_or_analysis_date")
        else:
            years = (facts.maturity_date - facts.as_of).days / 365.2425
            if years > self.preferred_maturity_years:
                preferences.append("maturity_exceeds_preferred_horizon_not_a_rejection")

        if rejects:
            status = ScreenStatus.REJECTED_BY_POLICY
        elif missing:
            status = ScreenStatus.NEEDS_EVIDENCE
        else:
            status = ScreenStatus.PASS_RESEARCH_ONLY
        return PolicyAssessment(
            status=status,
            reasons=tuple(dict.fromkeys(rejects)),
            missing_evidence=tuple(dict.fromkeys(missing)),
            preference_flags=tuple(dict.fromkeys(preferences)),
            policy_fingerprint=self.snapshot.fingerprint,
        )


def _canonical_grade(value: str) -> str | None:
    text = value.strip().upper().replace("−", "-").replace("–", "-").replace(" ", "")
    text = re.sub(r"\((?:RU|RUS)\)$", "", text)
    accepted = set(_rating_order())
    return text if text in accepted else None


def _rating_order() -> list[str]:
    # Ordered best to weakest only within a scale supplied by verified evidence.
    return [
        "AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB",
        "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC",
        "CCC-", "CC", "C", "D",
    ]
