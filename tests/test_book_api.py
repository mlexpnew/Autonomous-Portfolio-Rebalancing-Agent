from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from balancebook.backtest import evaluate
from balancebook.book import Book, load_clients, load_holdings


def test_book_from_csv_and_audit(tmp_path):
    pd.DataFrame(
        [{"client_id": "A", "name": "Asha", "risk": "Moderate", "equity": 55, "debt": 35, "gold": 10}]
    ).to_csv(tmp_path / "c.csv", index=False)
    pd.DataFrame(
        [
            {"client_id": "A", "asset": "equity", "units": 700, "cost": 60, "bought": "01/02/2021"},
            {"client_id": "A", "asset": "debt", "units": 250, "cost": 90, "bought": "2022-05-01"},
            {"client_id": "A", "asset": "gold", "units": 50, "cost": 80, "bought": "2022-05-01"},
        ]
    ).to_csv(tmp_path / "h.csv", index=False)
    book = Book(
        load_clients(tmp_path / "c.csv"),
        load_holdings(tmp_path / "h.csv"),
        {"equity": 100, "debt": 100, "gold": 100},
    )
    plans = book.plans(date(2026, 1, 10))
    assert plans[0].status == "rebalance"
    assert sum(x.units for x in book.holdings["A"]) == 1000  # proposals never change holdings
    book.decide("A", "2026-01-10", "approved", "Neha (RIA)")
    assert "approved" in book.audit_csv(tmp_path / "audit.csv").read_text()


def test_backtest_dev_gate():
    r = evaluate("dev")
    bb, th = r["balancebook"], r["threshold"]
    assert bb["total_tax_and_loads"] < 0.7 * th["total_tax_and_loads"]
    assert bb["months_outside"] <= th["months_outside"] + 0.02
    assert bb["after_tax_wealth"] > th["after_tax_wealth"]


def test_api():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from balancebook.api import app

    c = TestClient(app)
    body = {
        "client_id": "A",
        "risk": "moderate",
        "target": {"equity": 0.55, "debt": 0.35, "gold": 0.10},
        "prices": {"equity": 100, "debt": 100, "gold": 100},
        "as_of": "2026-01-10",
        "lots": [
            {"asset": "equity", "units": 700, "cost": 60, "bought": "2021-02-01"},
            {"asset": "debt", "units": 250, "cost": 90, "bought": "2022-05-01"},
            {"asset": "gold", "units": 50, "cost": 80, "bought": "2022-05-01"},
        ],
    }
    p = c.post("/v1/plan", json=body).json()
    assert p["status"] == "rebalance" and p["trades"]
    d = c.post(
        "/v1/decisions",
        json={"client_id": "A", "plan_date": "2026-01-10", "decision": "approved", "adviser": "Neha"},
    )
    assert d.status_code == 200 and len(c.get("/v1/decisions").json()) == 1


def test_dashboard():
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=60).run()
    assert not at.exception
