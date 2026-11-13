from datetime import date

from balancebook.engine import Client, Lot, plan, suitability, values, weights
from balancebook.tax import EQUITY_EXEMPTION, Gain, exit_load, fy, tax_on

P = {"equity": 100.0, "debt": 100.0, "gold": 100.0}
D = date(2026, 1, 15)


def test_tax_rules():
    assert fy(date(2026, 3, 31)) == 2025 and fy(date(2026, 4, 1)) == 2026
    t, used = tax_on([Gain("equity", 200_000, True)], 0.3, EQUITY_EXEMPTION)
    assert used == 125_000 and abs(t - 75_000 * 0.125 * 1.04) < 1e-6
    t, _ = tax_on([Gain("equity", 100_000, False)], 0.3, EQUITY_EXEMPTION)
    assert abs(t - 20_000 * 1.04) < 1e-6
    t, _ = tax_on([Gain("debt", 100_000, True)], 0.3, EQUITY_EXEMPTION)
    assert abs(t - 30_000 * 1.04) < 1e-6
    # a short-term loss offsets a long-term gain
    t, _ = tax_on([Gain("equity", -50_000, False), Gain("equity", 175_000, True)], 0.3, EQUITY_EXEMPTION)
    assert t == 0
    assert exit_load("equity", date(2025, 6, 1), D, 10_000) == 100
    assert exit_load("equity", date(2024, 6, 1), D, 10_000) == 0


def test_suitability_blocks_unsuitable_target():
    c = Client("C", "C", "conservative", {"equity": 0.6, "debt": 0.3, "gold": 0.1})
    assert suitability(c) and plan(c, [], P, D).status == "blocked"


def test_new_money_goes_to_underweight_first():
    c = Client("C", "C", "moderate", {"equity": 0.5, "debt": 0.4, "gold": 0.1})
    lots = [
        Lot("equity", 5300, 80, date(2022, 1, 1)),
        Lot("debt", 3700, 90, date(2022, 1, 1)),
        Lot("gold", 1000, 90, date(2022, 1, 1)),
    ]
    p = plan(c, lots, P, D, inflow=50_000)
    assert p.status == "hold"  # inside the band: no selling, no tax
    bought = {t.asset: t.amount for t in p.trades if t.action == "buy"}
    assert "equity" not in bought and bought["debt"] > 0


def test_tax_aware_lot_choice_avoids_short_term_and_exit_load():
    c = Client("C", "C", "moderate", {"equity": 0.5, "debt": 0.4, "gold": 0.1})
    lots = [
        Lot("equity", 3500, 50, date(2025, 10, 1)),  # recent: STCG + exit load
        Lot("equity", 3500, 95, date(2021, 1, 1)),  # old, small gain
        Lot("debt", 2000, 100, date(2023, 6, 1)),
        Lot("gold", 1000, 100, date(2022, 1, 1)),
    ]
    p = plan(c, lots, P, D)
    sold = [t for t in p.trades if t.action == "sell"]
    assert p.status == "rebalance" and sold and all(t.long_term for t in sold)
    assert p.exit_loads == 0 and p.est_tax == 0
    w = weights(values(lots, P))
    assert abs(w["equity"] - 0.5) <= 0.05
    fifo = plan(
        c,
        [
            Lot(x.asset, x.units, x.cost, x.bought)
            for x in [
                Lot("equity", 3500, 50, date(2025, 10, 1)),
                Lot("equity", 3500, 95, date(2021, 1, 1)),
                Lot("debt", 2000, 100, date(2023, 6, 1)),
                Lot("gold", 1000, 100, date(2022, 1, 1)),
            ]
        ],
        P,
        D,
        mode="threshold",
    )
    assert fifo.status == "rebalance"


def test_march_gain_harvest_inside_exemption():
    c = Client("C", "C", "moderate", {"equity": 0.5, "debt": 0.4, "gold": 0.1})
    lots = [
        Lot("equity", 5000, 50, date(2020, 1, 1)),
        Lot("debt", 4000, 100, date(2023, 6, 1)),
        Lot("gold", 1000, 100, date(2022, 1, 1)),
    ]
    p = plan(c, lots, P, date(2026, 3, 10))
    h = [t for t in p.trades if t.action == "harvest_sell"]
    assert p.status == "harvest" and abs(sum(t.gain for t in h) - 125_000) < 1 and p.est_tax == 0
    assert any(t.action == "harvest_buy" for t in p.trades)
    assert abs(sum(x.units for x in lots if x.asset == "equity") - 5000) < 1e-6  # same exposure
