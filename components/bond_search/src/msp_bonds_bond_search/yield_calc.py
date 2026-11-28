"""Conservative bond cash-flow helpers; these calculate scenarios, not advice."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import math
from typing import Iterable, Sequence


class YieldCalculationError(ValueError):
    """Inputs are incomplete, invalid, or do not admit a unique supported result."""


@dataclass(frozen=True)
class CashFlow:
    payment_date: date
    amount: Decimal


def _decimal(value: Decimal | str | int | float, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise YieldCalculationError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise YieldCalculationError(f"{field} must be finite")
    return result


def full_entry_cost(
    *,
    clean_price_percent: Decimal | str | int | float,
    applicable_nominal: Decimal | str | int | float,
    accrued_interest: Decimal | str | int | float,
    fees: Decimal | str | int | float,
) -> Decimal:
    price = _decimal(clean_price_percent, "clean_price_percent")
    nominal = _decimal(applicable_nominal, "applicable_nominal")
    accrued = _decimal(accrued_interest, "accrued_interest")
    commission = _decimal(fees, "fees")
    if price <= 0 or nominal <= 0 or accrued < 0 or commission < 0:
        raise YieldCalculationError("price and nominal must be positive; accrued interest and fees non-negative")
    return price * nominal / Decimal("100") + accrued + commission


def xirr(cash_flows: Sequence[CashFlow], *, max_rate: float = 1000.0) -> float:
    """Annualized ACT/365 XIRR as a decimal fraction, e.g. 0.12 means 12%.

    Requires a conventional bond flow with a negative initial investment and
    at least one later positive receipt. Missing or unknown payments must not
    be replaced with zero before calling.
    """
    if len(cash_flows) < 2:
        raise YieldCalculationError("at least an initial investment and one receipt are required")
    flows = sorted(cash_flows, key=lambda flow: flow.payment_date)
    amounts = [float(_decimal(flow.amount, "cash flow")) for flow in flows]
    if amounts[0] >= 0 or not any(amount > 0 for amount in amounts[1:]):
        raise YieldCalculationError("cash flows must start negative and include a later positive receipt")
    start = flows[0].payment_date
    if any(flow.payment_date < start for flow in flows):
        raise YieldCalculationError("cash flow dates are invalid")

    def npv(rate: float) -> float:
        if rate <= -1.0:
            return math.inf
        total = 0.0
        for flow, amount in zip(flows, amounts):
            years = (flow.payment_date - start).days / 365.0
            try:
                total += amount / ((1.0 + rate) ** years)
            except (OverflowError, ZeroDivisionError):
                return math.copysign(math.inf, amount)
        return total

    low, high = -0.9999, 0.1
    f_low, f_high = npv(low), npv(high)
    while f_low * f_high > 0 and high < max_rate:
        high = min(max_rate, high * 2 + 0.1)
        f_high = npv(high)
    if not math.isfinite(f_low) or not math.isfinite(f_high) or f_low * f_high > 0:
        raise YieldCalculationError("cash flows do not admit a supported XIRR bracket")
    for _ in range(200):
        mid = (low + high) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < 1e-10 or high - low < 1e-12:
            return mid
        if f_low * f_mid <= 0:
            high, f_high = mid, f_mid
        else:
            low, f_low = mid, f_mid
    raise YieldCalculationError("XIRR did not converge")


def bond_xirr(
    *,
    entry_date: date,
    total_entry_cost: Decimal | str | int | float,
    receipts: Iterable[CashFlow],
) -> float:
    cost = _decimal(total_entry_cost, "total_entry_cost")
    if cost <= 0:
        raise YieldCalculationError("total_entry_cost must be positive")
    future = list(receipts)
    if not future or any(flow.payment_date <= entry_date for flow in future):
        raise YieldCalculationError("all coupon/redemption receipts must be dated after entry")
    if any(_decimal(flow.amount, "receipt") <= 0 for flow in future):
        raise YieldCalculationError("receipts must be positive and explicitly known")
    flows = [CashFlow(entry_date, -cost), *future]
    return xirr(flows)
