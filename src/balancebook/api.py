"""HTTP API for an adviser back office.

uvicorn balancebook.api:app       # set BALANCEBOOK_API_KEYS to require X-API-Key
"""

from __future__ import annotations

import os
from datetime import date

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .engine import ASSETS, Client, Lot, plan, suitability

app = FastAPI(
    title="BalanceBook",
    version="1.0.0",
    description="Tax-aware rebalancing plans for Indian advisers. Nothing executes without approval.",
)
DECISIONS: list[dict] = []


def auth(x_api_key: str | None = Header(default=None)):
    keys = {k.strip() for k in os.getenv("BALANCEBOOK_API_KEYS", "").split(",") if k.strip()}
    if keys and x_api_key not in keys:
        raise HTTPException(401, "missing or invalid X-API-Key")


class LotIn(BaseModel):
    asset: str
    units: float = Field(gt=0)
    cost: float = Field(gt=0)
    bought: date


class PlanIn(BaseModel):
    client_id: str
    risk: str
    target: dict[str, float]
    band: float = 0.05
    slab: float = 0.30
    inflow: float = 0.0
    exemption_used: float = 0.0
    prices: dict[str, float]
    lots: list[LotIn]
    as_of: date | None = None


class DecisionIn(BaseModel):
    client_id: str
    plan_date: date
    decision: str
    adviser: str
    note: str = ""


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/plan", dependencies=[Depends(auth)])
def make_plan(body: PlanIn):
    if set(body.prices) < set(ASSETS):
        raise HTTPException(422, f"prices needed for {list(ASSETS)}")
    c = Client(body.client_id, body.client_id, body.risk.lower(), body.target, body.band, body.slab)
    lots = [Lot(x.asset.lower(), x.units, x.cost, x.bought) for x in body.lots]
    p = plan(c, lots, body.prices, body.as_of or date.today(), body.inflow, body.exemption_used)
    return p.as_dict() | {
        "suitability_issues": suitability(c),
        "note": "Proposal only. Execute after adviser approval.",
    }


@app.post("/v1/decisions", dependencies=[Depends(auth)])
def decide(d: DecisionIn):
    if d.decision not in ("approved", "rejected"):
        raise HTTPException(422, "decision must be approved or rejected")
    rec = d.model_dump(mode="json") | {"recorded_at": date.today().isoformat()}
    DECISIONS.append(rec)
    return rec


@app.get("/v1/decisions", dependencies=[Depends(auth)])
def decisions():
    return DECISIONS
