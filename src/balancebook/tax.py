"""Indian capital gains rules for mutual funds and ETFs (FY 2025-26 and FY 2026-27).

Source: Finance (No. 2) Act 2024 rates, unchanged by Budget 2026.
  Equity funds / ETFs (STT paid): held 12 months or less -> 20%; longer -> 12.5% on gains
    above ₹1.25 lakh per financial year (the exemption is shared across all equity sales).
  Debt funds bought on or after 1 April 2023: gains taxed at the investor's slab rate.
  Gold ETFs: held 12 months or less -> slab rate; longer -> 12.5%, no exemption.
  Health and education cess of 4% applies on top; surcharge is not modelled.
Every number lives here so an adviser can update it when the law changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

EQUITY_STCG = 0.20
EQUITY_LTCG = 0.125
EQUITY_EXEMPTION = 125_000
GOLD_LTCG = 0.125
LONG_TERM_DAYS = 365
CESS = 0.04
# exit loads: (days held below which the load applies, rate)
EXIT_LOAD = {"equity": (365, 0.01), "debt": (0, 0.0), "gold": (0, 0.0)}


def fy(d: date) -> int:
    """Financial year by its starting calendar year: 15 Jan 2026 -> 2025 (FY 2025-26)."""
    return d.year if d.month >= 4 else d.year - 1


def is_long(bought: date, sold: date) -> bool:
    return (sold - bought).days > LONG_TERM_DAYS


@dataclass
class Gain:
    asset: str
    amount: float  # gain (negative = loss)
    long_term: bool


def tax_on(gains: list[Gain], slab: float, exemption_left: float) -> tuple[float, float]:
    """Tax (with cess) on one financial year's realised gains, and exemption used.

    Losses offset gains of the same kind first (short-term losses may also offset long-term
    gains), as the Income Tax Act allows; the remaining equity LTCG gets the exemption."""
    b = {(g.asset, g.long_term): 0.0 for g in gains}
    for g in gains:
        b[(g.asset, g.long_term)] += g.amount
    eq_st, eq_lt = b.get(("equity", False), 0.0), b.get(("equity", True), 0.0)
    debt = b.get(("debt", False), 0.0) + b.get(("debt", True), 0.0)
    au_st, au_lt = b.get(("gold", False), 0.0), b.get(("gold", True), 0.0)
    st = {"equity": eq_st, "debt": debt, "gold": au_st}
    lt = {"equity": eq_lt, "gold": au_lt}
    # short-term losses offset short-term gains, then long-term gains
    st_loss = -sum(v for v in st.values() if v < 0)
    for k in st:
        st[k] = max(st[k], 0.0)
    for k in ("equity", "debt", "gold"):
        use = min(st_loss, st[k])
        st[k] -= use
        st_loss -= use
    for k in ("equity", "gold"):
        use = min(st_loss, max(lt[k], 0.0))
        lt[k] -= use
        st_loss -= use
    lt_loss = -sum(v for v in lt.values() if v < 0)
    for k in lt:
        lt[k] = max(lt[k], 0.0)
        use = min(lt_loss, lt[k])
        lt[k] -= use
        lt_loss -= use
    used = min(exemption_left, lt["equity"])
    tax = (
        st["equity"] * EQUITY_STCG
        + (lt["equity"] - used) * EQUITY_LTCG
        + st["debt"] * slab
        + st["gold"] * slab
        + lt["gold"] * GOLD_LTCG
    )
    return tax * (1 + CESS), used


def exit_load(asset: str, bought: date, sold: date, value: float) -> float:
    days, rate = EXIT_LOAD.get(asset, (0, 0.0))
    return value * rate if (sold - bought).days < days else 0.0
