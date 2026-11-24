"""Read-only single-ISIN dossier entry point for the existing MCP stdio host.

Stage 1 intentionally returns a COMPLETE *map* of required work, not a claim
that due diligence is complete. Sources and policy text are read afresh from
the existing authorized Google Workspace component; no new OAuth is created.
"""
from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone

# Public routing metadata ONLY; no private portfolio or rating thresholds.
# 27 checks from the approved dossier design, mapped to canonical rule IDs.
CHECKS = (
    ("issue_identity", "MOEX ISS", (1, 4, 59, 60, 68, 72, 74, 79)),
    ("issuer_identity", "ФНС — Прозрачный бизнес", (4, 24, 37, 45, 55, 78, 79)),
    ("coupon_terms", "НРД / NSD Data", (5, 11, 32, 35, 66, 80)),
    ("principal_amortization", "НРД / NSD Data", (7, 11, 32, 80)),
    ("offers_calls", "НРД / NSD Data", (6, 35, 80)),
    ("seniority_security", "Интерфакс — раскрытие", (37, 40, 80)),
    ("coupon_entitlement", "НРД / NSD Data", (36, 80)),
    ("issue_rating", "Эксперт РА", (17, 38, 61, 62, 81)),
    ("issuer_rating", "АКРА", (17, 38, 61, 62, 79, 81)),
    ("rating_history", "Банк России — рейтинговый репозитарий", (17, 19, 38, 39, 55, 56, 81)),
    ("payment_history", "НРД / NSD Data", (19, 44, 85)),
    ("financial_statements", "ФНС — БФО", (18, 41, 56, 82)),
    ("earnings_cashflow", "Официальный сайт эмитента", (18, 31, 41, 82, 83)),
    ("debt_cover", "Интерфакс — раскрытие", (18, 29, 31, 41, 83)),
    ("refinancing", "Официальный сайт эмитента", (28, 29, 41, 42, 84)),
    ("covenants", "Интерфакс — раскрытие", (37, 43, 84)),
    ("default_events", "Федресурс", (19, 31, 44, 85)),
    ("bankruptcy_cases", "Картотека арбитражных дел", (19, 45, 85)),
    ("stress_case", "Официальный сайт эмитента", (29, 30, 42, 71, 84, 88, 89)),
    ("float_or_step", "НРД / NSD Data", (33, 34, 35, 65, 66, 80)),
    ("current_quote", "MOEX ISS", (8, 16, 46, 70, 86)),
    ("market_depth", "MOEX ISS", (16, 46, 70, 86)),
    ("entry_cost", "MOEX ISS", (9, 10, 47, 60, 87)),
    ("cashflow_yield", "НРД / NSD Data", (11, 12, 13, 14, 15, 53, 87)),
    ("peers_benchmark", "MOEX ISS", (20, 48, 67, 87)),
    ("macro_context", "Банк России — ключевая ставка", (15, 42, 67, 87)),
    ("risk_conclusion", "Единая таблица «Портфель облигаций»", (22, 24, 31, 51, 54, 55, 71, 75, 76, 77, 78, 88, 89, 90)),
)
CONTEXT_ONLY = frozenset((2, 3, 21, 23, 25, 26, 27, 49, 50, 52, 57, 58, 63, 64, 69, 73, 91, 92, 93, 94, 95, 96))
# Rule 97 governs the entire second-level research workflow; mapping is not evidence.
CHECKS = tuple((code, source, ids + (97,)) for code, source, ids in CHECKS)


def valid_isin(isin: str) -> bool:
    """Validate ISO-6166 structure and Luhn check digit, not just prefix."""
    if not isinstance(isin, str) or len(isin) != 12 or not isin.startswith("RU"):
        return False
    if not all(c.isdigit() or ("A" <= c <= "Z") for c in isin):
        return False
    expanded = "".join(str(ord(c) - 55) if c.isalpha() else c for c in isin)
    total = 0
    for i, digit in enumerate(reversed(expanded)):
        value = int(digit) * (2 if i % 2 else 1)
        total += divmod(value, 10)[0] + divmod(value, 10)[1]
    return total % 10 == 0


def build_plan(snapshot, *, include_ledger: bool = False) -> dict:
    """Return source-routing plan; NEVER treat mappings as executed checks."""
    snapshot.assert_fresh()
    ids = [x.rule_id for x in snapshot.rules]
    if len(ids) < 97 or len(ids) > 500 or set(ids) != set(range(1, len(ids) + 1)):
        raise ValueError("CANONICAL_RULES_INCOMPLETE_OR_NOT_CONTIGUOUS")
    sources = {s.name: s for s in snapshot.resources}
    if len(sources) < 26:
        raise ValueError("CANONICAL_SOURCES_INCOMPLETE")
    missing = sorted({name for _, name, _ in CHECKS if name not in sources})
    if missing:
        raise ValueError("REQUIRED_SOURCE_MISSING")

    cover = {}
    for code, _, rule_ids in CHECKS:
        for n in rule_ids:
            cover.setdefault(n, []).append(code)
    no_route = tuple(n for n in ids if n not in cover and n not in CONTEXT_ONLY)
    if no_route:
        raise ValueError("RULES_WITHOUT_MAPPING_OR_CONTEXT")

    result = {
        "status": "RESEARCH_NOT_COMPLETED",
        "methodology": {
            "fingerprint": snapshot.fingerprint,
            "fetched_at": snapshot.fetched_at.isoformat(),
            "rules": len(ids),
            "registered_sources": len(sources),
            "mapped_checks": len(CHECKS),
            "executed_credit_checks": 0,
        },
        "checks": [
            {"code": code, "status": "NOT_COLLECTED", "planned_source": name,
             "rule_ids": list(rule_ids), "primary_evidence_verified": False}
            for code, name, rule_ids in CHECKS
        ],
        "source_status": "NOT_QUERIED",
        "gates": {"LEGAL": "INCOMPLETE", "CREDIT": "INCOMPLETE", "MARKET": "INCOMPLETE",
                  "FULL_DOSSIER": "NOT_READY"},
        "recommendation": "NONE",
        "limitations": [
            "The 27 mapped checks are planned, NOT performed.",
            "No rating, legal document, financial metrics, court search, quote, or cash flow is verified.",
            "A source URL or a successful ISIN lookup is not evidence of issuer solvency.",
        ],
    }
    if include_ledger:
        result["rule_ledger"] = [
            {"id": n, "policy_status": next(r.status for r in snapshot.rules if r.rule_id == n),
             "scope": "CONTEXT_OR_DEVELOPMENT" if n in CONTEXT_ONLY else "MAPPED_NOT_EXECUTED",
             "planned_checks": cover.get(n, [])}
            for n in ids
        ]
        result["source_ledger"] = [
            {"source": s.name, "status": "NOT_QUERIED",
             "related_checks": [code for code, name, _ in CHECKS if name == s.name]}
            for s in snapshot.resources
        ]
    return result


def analyze_bond(isin: str, *, check_moex: bool = False, include_ledger: bool = False) -> dict:
    """Run one explicitly invoked, read-only research-planning request.

    The connected HOME MCP host performs caller authorization; this function
    never accepts caller-supplied "verified" facts or changes any remote data.
    """
    if not valid_isin(isin):
        return {"status": "INVALID_ISIN", "recommendation": "NONE"}
    sheet_id = os.environ.get("MSP_BONDS_POLICY_SPREADSHEET_ID", "").strip()
    if not sheet_id:
        return {"status": "NOT_CONFIGURED",
                "reason": "MSP_BONDS_POLICY_SPREADSHEET_ID is not configured locally",
                "recommendation": "NONE"}
    try:
        from msp_bonds_bond_search.google_source import load_from_existing_workspace
        snapshot = load_from_existing_workspace(sheet_id)
        report = build_plan(snapshot, include_ledger=include_ledger)
    except Exception:
        # Never expose account identifiers, API responses, tokens or private paths.
        return {"status": "POLICY_UNAVAILABLE_OR_INVALID", "recommendation": "NONE"}

    report["isin"] = isin
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["moex_lookup"] = {"status": "NOT_REQUESTED"}
    if check_moex:
        try:
            from msp_bonds_bond_search.discovery import lookup_moex_isin
            match = lookup_moex_isin(isin)
            report["moex_lookup"] = {
                "status": match.state,
                "matched": match.matched,
                "security_id": match.security_id if match.matched else "",
                "short_name": match.short_name if match.matched else "",
                "source_url": match.source_url,
                "fetched_at": match.fetched_at.isoformat(),
                "scan_complete": match.scan_complete,
            }
        except Exception:
            report["moex_lookup"] = {"status": "INCONCLUSIVE", "reason": "MOEX_LOOKUP_FAILED"}
    return report
