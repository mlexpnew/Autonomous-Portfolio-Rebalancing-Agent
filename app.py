"""BalanceBook dashboard.  streamlit run app.py"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import streamlit as st

from balancebook.backtest import clients, market, opening_lots
from balancebook.book import Book, load_clients, load_holdings

st.set_page_config(page_title="BalanceBook", page_icon="⚖️", layout="wide")
st.title("BalanceBook")
st.caption(
    "Tax-aware rebalancing for Indian advisers: new money first, trades only when a client "
    "leaves their band, cheapest lots first, March gain harvesting. Nothing executes without approval."
)

cu = st.sidebar.file_uploader("Clients (CSV)", type="csv")
ho = st.sidebar.file_uploader("Holdings (CSV)", type="csv")
st.sidebar.write("Current NAVs")
pe = st.sidebar.number_input("Equity", value=100.0)
pdbt = st.sidebar.number_input("Debt", value=100.0)
pg = st.sidebar.number_input("Gold", value=100.0)
today = st.sidebar.date_input("Plan date", date(2026, 3, 10))


@st.cache_data
def sample():
    m = market(11)
    cl, hold = [], {}
    for k, (c, v, dr) in enumerate(clients(21, n=40)):
        cl.append(c)
        hold[c.client_id] = opening_lots(c, v, dr, m, np.random.default_rng(k))
    return cl, hold


if cu and ho:
    cl, hold = load_clients(cu), load_holdings(ho)
else:
    st.info("No files uploaded: showing a sample book of 40 clients.")
    cl, hold = sample()
book = Book(cl, hold, {"equity": pe, "debt": pdbt, "gold": pg})
if "decisions" not in st.session_state:
    st.session_state.decisions = []
plans = book.plans(today)

k = st.columns(4)
counts = pd.Series([p.status for p in plans]).value_counts()
k[0].metric("Clients", len(plans))
k[1].metric("Need rebalancing", int(counts.get("rebalance", 0)))
k[2].metric("Gain harvesting", int(counts.get("harvest", 0)))
k[3].metric("Blocked (suitability)", int(counts.get("blocked", 0)))

st.dataframe(
    pd.DataFrame(
        [
            {
                "client": p.client_id,
                "status": p.status,
                "equity now": p.weights_before.get("equity"),
                "equity after": p.weights_after.get("equity"),
                "est. tax": round(p.est_tax),
                "exit loads": round(p.exit_loads),
                "why": "; ".join(p.reasons),
            }
            for p in plans
        ]
    ),
    hide_index=True,
    use_container_width=True,
)

pick = st.selectbox("Review a plan", [p.client_id for p in plans if p.status != "hold"] or [plans[0].client_id])
p = next(x for x in plans if x.client_id == pick)
st.write(f"**{p.status}**: " + "; ".join(p.reasons))
if p.trades:
    st.dataframe(
        pd.DataFrame([t.as_dict() for t in p.trades]).round(2), hide_index=True, use_container_width=True
    )
c1, c2, c3 = st.columns([2, 1, 1])
adviser = c1.text_input("Adviser", "")
if c2.button("Approve", disabled=not adviser):
    st.session_state.decisions.append(book.decide(pick, str(today), "approved", adviser).__dict__)
if c3.button("Reject", disabled=not adviser):
    st.session_state.decisions.append(book.decide(pick, str(today), "rejected", adviser).__dict__)
if st.session_state.decisions:
    st.subheader("Approval log")
    st.dataframe(pd.DataFrame(st.session_state.decisions), hide_index=True)
