"""The adviser's book: clients and holdings from CSV/Excel, plans for everyone, an approval log.

clients:  client_id, name, risk (conservative/moderate/aggressive), equity, debt, gold
          (target weights, 0-1 or %), [band], [slab], [sip]
holdings: client_id, asset (equity/debt/gold), units, cost (per unit), bought (date)
prices:   asset -> current NAV
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .engine import ASSETS, Client, Lot, Plan, plan


def _read(p) -> pd.DataFrame:
    if isinstance(p, pd.DataFrame):
        return p.copy()
    p = Path(p)
    return pd.read_excel(p) if p.suffix.lower() in (".xlsx", ".xls") else pd.read_csv(p)


def load_clients(p) -> list[Client]:
    df = _read(p)
    out = []
    for r in df.to_dict(orient="records"):
        t = {a: float(r[a]) for a in ASSETS}
        if sum(t.values()) > 1.5:  # given in percent
            t = {a: v / 100 for a, v in t.items()}
        out.append(
            Client(
                str(r["client_id"]),
                str(r.get("name", r["client_id"])),
                str(r["risk"]).lower(),
                t,
                float(r.get("band", 0.05) or 0.05),
                float(r.get("slab", 0.3) or 0.3),
                float(r.get("sip", 0) or 0),
            )
        )
    return out


def load_holdings(p) -> dict[str, list[Lot]]:
    df = _read(p)
    raw = df["bought"].astype(str).str.strip()
    iso = raw.str.match(r"^\d{4}-\d{2}-\d{2}")  # ISO as written; others day-first (Indian style)
    parsed = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    parsed[iso] = pd.to_datetime(raw[iso], format="ISO8601")
    parsed[~iso] = pd.to_datetime(raw[~iso], format="mixed", dayfirst=True)
    df["bought"] = parsed.dt.date
    out: dict[str, list[Lot]] = {}
    for r in df.to_dict(orient="records"):
        out.setdefault(str(r["client_id"]), []).append(
            Lot(str(r["asset"]).lower(), float(r["units"]), float(r["cost"]), r["bought"])
        )
    return out


@dataclass
class Decision:
    client_id: str
    plan_date: str
    decision: str  # approved | rejected
    adviser: str
    note: str
    at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class Book:
    def __init__(self, clients: list[Client], holdings: dict[str, list[Lot]], prices: dict[str, float]):
        self.clients = {c.client_id: c for c in clients}
        self.holdings = holdings
        self.prices = prices
        self.decisions: list[Decision] = []

    def plans(self, today: date | None = None, exemption_used: dict[str, float] | None = None) -> list[Plan]:
        """Proposals for every client. Works on copies: nothing changes until approved."""
        today = today or date.today()
        out = []
        for cid, c in self.clients.items():
            lots = [Lot(x.asset, x.units, x.cost, x.bought) for x in self.holdings.get(cid, [])]
            out.append(
                plan(
                    c,
                    lots,
                    self.prices,
                    today,
                    inflow=c.sip,
                    exemption_used=(exemption_used or {}).get(cid, 0.0),
                )
            )
        order = {"blocked": 0, "rebalance": 1, "harvest": 2, "hold": 3}
        return sorted(out, key=lambda p: order[p.status])

    def decide(self, client_id: str, plan_date: str, decision: str, adviser: str, note: str = "") -> Decision:
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved or rejected")
        d = Decision(client_id, plan_date, decision, adviser, note)
        self.decisions.append(d)
        return d

    def audit_csv(self, path: str | Path) -> Path:
        path = Path(path)
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["client_id", "plan_date", "decision", "adviser", "note", "at"])
            w.writeheader()
            for d in self.decisions:
                w.writerow(d.__dict__)
        return path
