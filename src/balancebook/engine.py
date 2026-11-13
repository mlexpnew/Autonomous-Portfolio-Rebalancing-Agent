"""Rebalancing plans an adviser can approve: what to buy and sell, which lots, why, and what
it costs in tax and exit loads. Nothing is executed without approval."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .tax import EQUITY_EXEMPTION, EQUITY_LTCG, EQUITY_STCG, GOLD_LTCG, Gain, exit_load, fy, is_long

ASSETS = ("equity", "debt", "gold")
# SEBI-style suitability: the highest equity weight each risk profile may hold
MAX_EQUITY = {"conservative": 0.40, "moderate": 0.70, "aggressive": 0.90}


@dataclass
class Lot:
    asset: str
    units: float
    cost: float  # per unit
    bought: date


@dataclass
class Client:
    client_id: str
    name: str
    risk: str
    target: dict[str, float]
    band: float = 0.05  # absolute drift allowed per asset (5 percentage points)
    slab: float = 0.30  # income-tax slab for debt and short-term gold gains
    sip: float = 0.0  # monthly inflow


@dataclass
class Trade:
    action: str  # buy | sell | harvest_sell | harvest_buy
    asset: str
    amount: float
    lot_bought: date | None = None
    gain: float = 0.0
    long_term: bool | None = None
    exit_load: float = 0.0
    reason: str = ""

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["lot_bought"] = str(self.lot_bought) if self.lot_bought else None
        return d


@dataclass
class Plan:
    client_id: str
    date: date
    status: str  # hold | rebalance | harvest | blocked
    trades: list[Trade] = field(default_factory=list)
    weights_before: dict[str, float] = field(default_factory=dict)
    weights_after: dict[str, float] = field(default_factory=dict)
    est_tax: float = 0.0
    exit_loads: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "client_id": self.client_id,
            "date": str(self.date),
            "status": self.status,
            "weights_before": self.weights_before,
            "weights_after": self.weights_after,
            "est_tax": round(self.est_tax, 2),
            "exit_loads": round(self.exit_loads, 2),
            "reasons": self.reasons,
            "trades": [t.as_dict() for t in self.trades],
        }


def values(lots: list[Lot], prices: dict[str, float]) -> dict[str, float]:
    v = {a: 0.0 for a in ASSETS}
    for lot in lots:
        v[lot.asset] += lot.units * prices[lot.asset]
    return v


def weights(v: dict[str, float]) -> dict[str, float]:
    tot = sum(v.values()) or 1.0
    return {a: v[a] / tot for a in ASSETS}


def suitability(c: Client) -> list[str]:
    errs = []
    if abs(sum(c.target.values()) - 1) > 1e-6:
        errs.append("target weights must add up to 100%")
    cap = MAX_EQUITY.get(c.risk)
    if cap is not None and c.target.get("equity", 0) > cap + 1e-9:
        errs.append(f"{c.target['equity']:.0%} equity exceeds the {cap:.0%} limit for a {c.risk} profile")
    return errs


def _cost_per_rupee(lot: Lot, price: float, today: date, slab: float, exemption_left: float) -> float:
    """Tax plus exit load per rupee sold from this lot (negative for losses: they save tax)."""
    frac = (price - lot.cost) / price
    lt = is_long(lot.bought, today)
    if lot.asset == "equity":
        rate = (0.0 if exemption_left > 0 else EQUITY_LTCG) if lt else EQUITY_STCG
    elif lot.asset == "gold":
        rate = GOLD_LTCG if lt else slab
    else:
        rate = slab
    load = exit_load(lot.asset, lot.bought, today, 1.0)
    return frac * rate + load


def _sell(
    lots: list[Lot],
    asset: str,
    amount: float,
    price: float,
    today: date,
    slab: float,
    exemption_left: float,
    tax_aware: bool,
    reason: str,
) -> tuple[list[Trade], float]:
    """Sell `amount` of an asset. Tax-aware: cheapest lots first; otherwise oldest first (FIFO)."""
    pool = [x for x in lots if x.asset == asset and x.units > 1e-9]
    if tax_aware:
        pool.sort(key=lambda x: (_cost_per_rupee(x, price, today, slab, exemption_left), x.bought))
    else:
        pool.sort(key=lambda x: x.bought)
    trades, left = [], amount
    for lot in pool:
        if left <= 1e-6:
            break
        take = min(left, lot.units * price)
        units = take / price
        gain = (price - lot.cost) * units
        lt = is_long(lot.bought, today)
        load = exit_load(asset, lot.bought, today, take)
        if tax_aware and asset == "equity" and lt and gain > 0:
            exemption_left -= min(gain, exemption_left)
        lot.units -= units
        trades.append(Trade("sell", asset, take, lot.bought, gain, lt, load, reason))
        left -= take
    return trades, exemption_left


def plan(
    c: Client,
    lots: list[Lot],
    prices: dict[str, float],
    today: date,
    inflow: float = 0.0,
    exemption_used: float = 0.0,
    mode: str = "balancebook",
) -> Plan:
    """Build (and apply to `lots`) the month's plan.

    mode: balancebook (tax-aware), threshold (full rebalance when outside band, FIFO),
          calendar (full rebalance every April, FIFO), none (SIP split by target only).
    """
    p = Plan(c.client_id, today, "hold")
    errs = suitability(c)
    if errs:
        p.status, p.reasons = "blocked", errs
        return p
    v = values(lots, prices)
    p.weights_before = {a: round(x, 4) for a, x in weights(v).items()}
    total = sum(v.values()) + inflow
    exemption_left = max(0.0, EQUITY_EXEMPTION - exemption_used)

    # 1. new money
    buys = {a: 0.0 for a in ASSETS}
    if inflow > 0:
        if mode == "balancebook":  # into whatever is most underweight
            gaps = {a: max(0.0, c.target[a] * total - v[a]) for a in ASSETS}
            g = sum(gaps.values())
            share = {a: gaps[a] / g for a in ASSETS} if g > 0 else c.target
        else:
            share = c.target
        for a in ASSETS:
            buys[a] += inflow * share[a]
            v[a] += inflow * share[a]
        p.reasons.append(
            "new money directed to underweight assets" if mode == "balancebook" else "new money split by target"
        )

    w = weights(v)
    drift = {a: w[a] - c.target[a] for a in ASSETS}
    outside = any(abs(d) > c.band for d in drift.values())
    if mode == "none":
        due = False
    elif mode == "calendar":
        due = today.month == 4
    else:
        due = outside

    sells = []
    if due:
        p.status = "rebalance"
        total = sum(v.values())
        if mode == "balancebook":
            # trade only back to halfway inside the band, not all the way to target
            goal = {}
            for a in ASSETS:
                if drift[a] > c.band:
                    goal[a] = c.target[a] + c.band / 2
                elif drift[a] < -c.band:
                    goal[a] = c.target[a] - c.band / 2
                else:
                    goal[a] = w[a]
            # sell enough to bring every out-of-band asset back to halfway inside its band:
            # overweight assets down to their goal, and enough to lift underweight ones to theirs
            to_cut = sum(max(0.0, (w[a] - goal[a]) * total) for a in ASSETS)
            to_fill = sum(max(0.0, (goal[a] - w[a]) * total) for a in ASSETS)
            need = max(to_cut, to_fill)
            excess = {a: max(0.0, w[a] - c.target[a]) for a in ASSETS}
            ex = sum(excess.values()) or 1.0
            over = {a: need * excess[a] / ex for a in ASSETS}
            under = {a: max(0.0, (c.target[a] - w[a]) * total) for a in ASSETS if w[a] < c.target[a]}
            u = sum(under.values())
            p.reasons.append(
                "outside band: " + ", ".join(f"{a} {drift[a]:+.1%}" for a in ASSETS if abs(drift[a]) > c.band)
            )
        else:
            over = {a: max(0.0, (w[a] - c.target[a]) * total) for a in ASSETS}
            need = sum(over.values())
            under = {a: max(0.0, (c.target[a] - w[a]) * total) for a in ASSETS}
            u = sum(under.values())
            p.reasons.append(
                "calendar rebalance to target"
                if mode == "calendar"
                else "outside band: full rebalance to target"
            )
        for a in ASSETS:
            if over.get(a, 0) > 1:
                tr, exemption_left = _sell(
                    lots,
                    a,
                    over[a],
                    prices[a],
                    today,
                    c.slab,
                    exemption_left,
                    mode == "balancebook",
                    "reduce overweight",
                )
                sells += tr
        proceeds = sum(t.amount - t.exit_load for t in sells)
        for a, gap in under.items():
            if u > 0:
                buys[a] += proceeds * gap / u

    # 2. March: book tax-free long-term equity gains up to the yearly exemption, then rebuy
    harvest = []
    if mode == "balancebook" and today.month == 3 and exemption_left > 5_000:
        pool = sorted(
            [
                x
                for x in lots
                if x.asset == "equity"
                and x.units > 1e-9
                and is_long(x.bought, today)
                and prices["equity"] > x.cost
            ],
            key=lambda x: x.bought,
        )
        room = exemption_left
        for lot in pool:
            if room <= 1:
                break
            gain_per_unit = prices["equity"] - lot.cost
            units = min(lot.units, room / gain_per_unit)
            amt = units * prices["equity"]
            lot.units -= units
            harvest.append(
                Trade(
                    "harvest_sell",
                    "equity",
                    amt,
                    lot.bought,
                    gain_per_unit * units,
                    True,
                    0.0,
                    "book gains inside the ₹1.25 lakh yearly exemption",
                )
            )
            room -= gain_per_unit * units
        if harvest:
            amt = sum(t.amount for t in harvest)
            harvest.append(
                Trade(
                    "harvest_buy",
                    "equity",
                    amt,
                    None,
                    reason="buy back at today's price (raises the cost basis; India has no wash-sale rule)",
                )
            )
            if p.status == "hold":
                p.status = "harvest"
            p.reasons.append(f"harvested {sum(t.gain for t in harvest):,.0f} of tax-free long-term gains")

    # apply buys
    for a in ASSETS:
        if buys[a] > 1:
            lots.append(Lot(a, buys[a] / prices[a], prices[a], today))
            p.trades.append(Trade("buy", a, buys[a], reason="new money" if not sells else "rebalance"))
    for t in harvest:
        if t.action == "harvest_buy":
            lots.append(Lot("equity", t.amount / prices["equity"], prices["equity"], today))
    p.trades = sells + harvest + p.trades
    lots[:] = [x for x in lots if x.units > 1e-9]
    gains = [Gain(t.asset, t.gain, bool(t.long_term)) for t in sells + harvest if t.action != "harvest_buy"]
    p.exit_loads = sum(t.exit_load for t in sells)
    from .tax import tax_on

    p.est_tax, _ = tax_on(gains, c.slab, max(0.0, EQUITY_EXEMPTION - exemption_used))
    p.weights_after = {a: round(x, 4) for a, x in weights(values(lots, prices)).items()}
    return p


def realised(p: Plan) -> list[Gain]:
    return [Gain(t.asset, t.gain, bool(t.long_term)) for t in p.trades if t.action in ("sell", "harvest_sell")]


__all__ = [
    "ASSETS",
    "Client",
    "Lot",
    "Plan",
    "Trade",
    "fy",
    "plan",
    "realised",
    "suitability",
    "values",
    "weights",
]
