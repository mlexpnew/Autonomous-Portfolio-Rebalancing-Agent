"""Command line.

balancebook plan clients.csv holdings.csv --prices equity=245.1,debt=31.8,gold=72.4 --out plans.xlsx
balancebook demo --out demo/        # sample clients.csv and holdings.csv
balancebook backtest
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from .book import Book, load_clients, load_holdings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="balancebook")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("clients")
    p.add_argument("holdings")
    p.add_argument("--prices", required=True, help="equity=..,debt=..,gold=..")
    p.add_argument("--out", default="balancebook_plans.xlsx")
    d = sub.add_parser("demo")
    d.add_argument("--out", default="demo")
    sub.add_parser("backtest")
    a = ap.parse_args(argv)
    if a.cmd == "backtest":
        from .backtest import main as bt

        return bt([])
    if a.cmd == "demo":
        from .backtest import clients, market, opening_lots

        m = market(11)
        rows, hold = [], []
        for k, (c, v, dr) in enumerate(clients(21, n=40)):
            rows.append(
                {
                    "client_id": c.client_id,
                    "name": c.name,
                    "risk": c.risk,
                    **c.target,
                    "band": c.band,
                    "slab": c.slab,
                    "sip": c.sip,
                }
            )
            for lot in opening_lots(c, v, dr, m, np.random.default_rng(k)):
                hold.append(
                    {
                        "client_id": c.client_id,
                        "asset": lot.asset,
                        "units": round(lot.units, 3),
                        "cost": round(lot.cost, 2),
                        "bought": lot.bought,
                    }
                )
        Path(a.out).mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(Path(a.out) / "clients.csv", index=False)
        pd.DataFrame(hold).to_csv(Path(a.out) / "holdings.csv", index=False)
        print(
            f"wrote {len(rows)} clients and {len(hold)} lots to {a.out}/ (prices: equity=100,debt=100,gold=100)"
        )
        return 0
    prices = {k: float(v) for k, v in (x.split("=") for x in a.prices.split(","))}
    book = Book(load_clients(a.clients), load_holdings(a.holdings), prices)
    plans = book.plans()
    with pd.ExcelWriter(a.out) as xw:
        pd.DataFrame(
            [
                {
                    "client_id": p.client_id,
                    "status": p.status,
                    "est_tax": round(p.est_tax),
                    "exit_loads": round(p.exit_loads),
                    "reasons": "; ".join(p.reasons),
                    **{f"{k}_before": v for k, v in p.weights_before.items()},
                    **{f"{k}_after": v for k, v in p.weights_after.items()},
                }
                for p in plans
            ]
        ).to_excel(xw, sheet_name="Plans", index=False)
        pd.DataFrame([{"client_id": p.client_id, **t.as_dict()} for p in plans for t in p.trades]).to_excel(
            xw, sheet_name="Trades", index=False
        )
    counts = pd.Series([p.status for p in plans]).value_counts().to_dict()
    print(f"{len(plans)} clients: {counts} -> {a.out} (nothing executed; approve each plan)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
