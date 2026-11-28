import unittest
from datetime import date
from decimal import Decimal

from msp_bonds_bond_search.yield_calc import (
    CashFlow,
    YieldCalculationError,
    bond_xirr,
    full_entry_cost,
    xirr,
)


class YieldCalculationTests(unittest.TestCase):
    def test_full_entry_cost_includes_clean_price_accrued_interest_and_fees(self):
        total = full_entry_cost(
            clean_price_percent="99.5",
            applicable_nominal="1000",
            accrued_interest="2.50",
            fees="1.00",
        )
        self.assertEqual(total, Decimal("998.500"))

    def test_full_entry_cost_uses_actual_nominal_and_accepts_premium_price(self):
        total = full_entry_cost(
            clean_price_percent="103",
            applicable_nominal="2000",
            accrued_interest="0",
            fees="0",
        )
        self.assertEqual(total, Decimal("2060"))

    def test_xirr_for_one_year_known_cashflow(self):
        result = xirr([
            CashFlow(date(2025, 1, 1), Decimal("-1000")),
            CashFlow(date(2026, 1, 1), Decimal("1100")),
        ])
        self.assertAlmostEqual(result, (1.1 ** (365 / 365.0)) - 1, places=7)

    def test_bond_xirr_requires_explicit_future_receipts(self):
        result = bond_xirr(
            entry_date=date(2025, 1, 1),
            total_entry_cost=Decimal("1000"),
            receipts=[CashFlow(date(2026, 1, 1), Decimal("1100"))],
        )
        self.assertGreater(result, 0.099)
        self.assertLess(result, 0.101)

    def test_unknown_or_nonpositive_future_receipt_is_not_filled_with_zero(self):
        with self.assertRaises(YieldCalculationError):
            bond_xirr(
                entry_date=date(2025, 1, 1),
                total_entry_cost=1000,
                receipts=[CashFlow(date(2026, 1, 1), Decimal("0"))],
            )

    def test_cost_rejects_negative_accrued_interest_and_nonfinite_inputs(self):
        with self.assertRaises(YieldCalculationError):
            full_entry_cost(
                clean_price_percent=100,
                applicable_nominal=1000,
                accrued_interest=-1,
                fees=0,
            )
        with self.assertRaises(YieldCalculationError):
            full_entry_cost(
                clean_price_percent=float("nan"),
                applicable_nominal=1000,
                accrued_interest=0,
                fees=0,
            )


if __name__ == "__main__":
    unittest.main()
