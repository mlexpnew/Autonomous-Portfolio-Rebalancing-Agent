"""Ten-year backtest on simulated markets and client books.

    python -m balancebook.backtest --gate

Strategies, run on the same clients and the same market path:
  none        SIP split by target, never rebalance
  calendar    every April, sell and buy back to target, oldest lots first (common practice)
  threshold   when any asset drifts more than 5 points, full rebalance to target, oldest lots
  balancebook new money to underweight assets first, trade only back inside the band,
              cheapest lots first (tax and exit load), March gain harvesting
Scored per client: how far from target it stayed, tax and exit loads paid, and after-tax
wealth if everything were sold at the end. The held-out market has two crashes and higher
volatility; the held-out book has a different client mix. It was not used for tuning.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np

from .engine import ASSETS, Client, Lot, plan, realised, values, weights
from .tax import EQUITY_EXEMPTION, Gain, fy, tax_on

TARGETS = {
    "conservative": {"equity": 0.30, "debt": 0.60, "gold": 0.10},
    "moderate": {"equity": 0.55, "debt": 0.35, "gold": 0.10},
    "aggressive": {"equity": 0.75, "debt": 0.15, "gold": 0.10},
}
STRATEGIES = ("none", "calendar", "threshold", "balancebook")


@dataclass
class Market:
    months: list[date]
    prices: list[dict[str, float]]


def market(
    seed: int, years: int = 10, crashes: int = 1, vol_mult: float = 1.0, start: date = date(2016, 1, 1)
) -> Market:
    rng = np.random.default_rng(seed)
    mu = np.array([0.12, 0.07, 0.09]) / 12
    vol = np.array([0.17, 0.025, 0.14]) / np.sqrt(12) * vol_mult
    corr = np.array([[1, 0.1, -0.1], [0.1, 1, 0.1], [-0.1, 0.1, 1]])
    cov = np.outer(vol, vol) * corr
    n = years * 12
    r = rng.multivariate_normal(mu - np.diag(cov) / 2, cov, size=n)
    for k in range(crashes):
        at = int(rng.integers(12 + k * 40, min(n - 12, 50 + k * 40)))
        r[at : at + 3, 0] += np.log(0.78) / 3  # equity falls about 22% over a quarter
        r[at : at + 3, 2] += np.log(1.06) / 3  # gold rises
    px = np.exp(np.cumsum(r, axis=0)) * np.array([100.0, 100.0, 100.0])
    months = [
        date(start.year + (start.month - 1 + i) // 12, (start.month - 1 + i) % 12 + 1, 1) for i in range(n)
    ]
    return Market(months, [dict(zip(ASSETS, row, strict=True)) for row in px])


def clients(
    seed: int, n: int = 300, mix=(0.3, 0.45, 0.25), sip_hi: float = 1e5
) -> list[tuple[Client, float, float]]:
    """(client, starting value, starting drift scale)."""
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        risk = rng.choice(list(TARGETS), p=list(mix))
        c = Client(
            f"CL{i:04d}",
            f"Client {i}",
            str(risk),
            dict(TARGETS[risk]),
            0.05,
            float(rng.choice([0.2, 0.3, 0.3])),
            float(round(rng.uniform(0, sip_hi), -3)),
        )
        out.append((c, float(rng.uniform(2e6, 2e7)), float(rng.uniform(0, 0.12))))
    return out


def opening_lots(c: Client, value: float, drift: float, m: Market, rng) -> list[Lot]:
    """An existing book: allocation off target, lots bought over the previous three years."""
    w = np.array([c.target[a] for a in ASSETS]) + rng.normal(0, drift, 3)
    w = np.clip(w, 0.02, None)
    w = w / w.sum()
    lots = []
    p0 = m.prices[0]
    for a, wa in zip(ASSETS, w, strict=True):
        for k in range(4):
            bought = date(m.months[0].year - 3 + k % 3, 1 + (k * 4) % 12, 1)
            cost = p0[a] * float(rng.uniform(0.6, 1.0))
            lots.append(Lot(a, value * wa / 4 / p0[a], cost, bought))
    return lots


def run_client(c: Client, value: float, drift: float, m: Market, strategy: str, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    lots = opening_lots(c, value, drift, m, rng)
    gains_fy: dict[int, list[Gain]] = {}
    tax_paid = loads = 0.0
    dist, outside, trades = [], 0, 0
    cur_fy = fy(m.months[0])
    for i, (d, px) in enumerate(zip(m.months, m.prices, strict=True)):
        if fy(d) != cur_fy:  # pay last year's tax
            t, _ = tax_on(gains_fy.get(cur_fy, []), c.slab, EQUITY_EXEMPTION)
            tax_paid += t
            cur_fy = fy(d)
        used = sum(
            g.amount for g in gains_fy.get(cur_fy, []) if g.asset == "equity" and g.long_term and g.amount > 0
        )
        p = plan(
            c,
            lots,
            px,
            d,
            inflow=c.sip if i > 0 else 0.0,
            exemption_used=min(used, EQUITY_EXEMPTION),
            mode=strategy,
        )
        gains_fy.setdefault(cur_fy, []).extend(realised(p))
        loads += p.exit_loads
        trades += sum(1 for t in p.trades if t.action != "buy" or p.status != "hold")
        w = weights(values(lots, px))
        dist.append(0.5 * sum(abs(w[a] - c.target[a]) for a in ASSETS))
        outside += any(abs(w[a] - c.target[a]) > c.band for a in ASSETS)
    # sell everything at the end, in the final financial year, for an after-tax comparison
    px, d = m.prices[-1], m.months[-1]
    final = [Gain(x.asset, (px[x.asset] - x.cost) * x.units, (d - x.bought).days > 365) for x in lots]
    t_final, _ = tax_on(gains_fy.get(cur_fy, []) + final, c.slab, EQUITY_EXEMPTION)
    gross = sum(values(lots, px).values())
    return {
        "avg_distance": float(np.mean(dist)),
        "months_outside": outside / len(m.months),
        "tax_paid": tax_paid,
        "exit_loads": loads,
        "after_tax_wealth": gross - t_final - tax_paid,
        "trades": trades,
    }


def evaluate(which: str) -> dict:
    if which == "dev":
        m, book = market(11), clients(21)
    else:
        m, book = (
            market(12, crashes=2, vol_mult=1.3, start=date(2015, 4, 1)),
            clients(22, mix=(0.45, 0.35, 0.2), sip_hi=2e5),
        )
    res = {}
    for s in STRATEGIES:
        rows = [run_client(c, v, dr, m, s, seed=k) for k, (c, v, dr) in enumerate(book)]
        res[s] = {k: float(np.mean([r[k] for r in rows])) for k in rows[0]}
        res[s]["total_tax_and_loads"] = res[s]["tax_paid"] + res[s]["exit_loads"]
        if s == "balancebook":
            res["_rows"] = rows
    bb = res.pop("_rows")
    th = [run_client(c, v, dr, m, "threshold", seed=k) for k, (c, v, dr) in enumerate(book)]
    res["balancebook_beats_threshold_after_tax"] = float(
        np.mean([a["after_tax_wealth"] > b["after_tax_wealth"] for a, b in zip(bb, th, strict=True)])
    )
    res["clients"] = len(book)
    return res


def lakh(x: float) -> str:
    return f"₹{x / 1e5:,.2f} lakh"


def report(r: dict) -> str:
    lines = [
        "| Strategy (average per client) | Distance from target, dev / held-out | Months outside band | "
        "Tax + exit loads paid | After-tax wealth at the end |",
        "|---|---|---|---|---|",
    ]
    for s, label in (
        ("none", "Never rebalance"),
        ("calendar", "Every April, to target"),
        ("threshold", "5-point band, to target"),
        ("balancebook", "**BalanceBook**"),
    ):
        a, b = r["dev"][s], r["held_out"][s]
        lines.append(
            f"| {label} | {a['avg_distance']:.1%} / {b['avg_distance']:.1%} | "
            f"{a['months_outside']:.0%} / {b['months_outside']:.0%} | "
            f"{lakh(a['total_tax_and_loads'])} / {lakh(b['total_tax_and_loads'])} | "
            f"{lakh(a['after_tax_wealth'])} / {lakh(b['after_tax_wealth'])} |"
        )
    return (
        "\n".join(
            lines
            + [
                "",
                f"BalanceBook ends with more after-tax wealth than the 5-point band "
                f"strategy for {r['dev']['balancebook_beats_threshold_after_tax']:.0%} of "
                f"clients (dev) and {r['held_out']['balancebook_beats_threshold_after_tax']:.0%} "
                "(held-out).",
            ]
        )
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest BalanceBook")
    ap.add_argument("--out", default="eval_results")
    ap.add_argument("--gate", action="store_true")
    a = ap.parse_args(argv)
    r = {"dev": evaluate("dev"), "held_out": evaluate("held_out")}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(r, indent=2))
    md = report(r)
    (out / "report.md").write_text(md)
    print(md)
    if a.gate:
        d = r["dev"]
        ok = (
            d["balancebook"]["total_tax_and_loads"] < d["threshold"]["total_tax_and_loads"]
            and d["balancebook"]["months_outside"] <= d["threshold"]["months_outside"] + 0.02
            and d["balancebook"]["after_tax_wealth"] > d["threshold"]["after_tax_wealth"]
        )
        if not ok:
            print("gate failed", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
