"""The live rules (alerts/rules.step) replayed day by day must reproduce the research backtest
(pivot-live/tools/stock_swing_wf.sim, "heavy 12" universe from stock_swing_ndx) without costs."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT.parent / "pivot-live" / "tools"
pytestmark = pytest.mark.skipif(not (RESEARCH.parent / "data" / "yf_ndx_px.csv").exists(), reason="research data missing")

CFG = {"buy_usd": 3000, "cap_usd": 30000, "max_buys": 3, "max_days": 5}


def test_replay_matches_backtest():
    sys.path.insert(0, str(RESEARCH)); sys.argv = ["x"]
    import stock_swing_ndx as nd
    from alerts import rules
    wf, adj = nd.wf, nd.adj
    years = range(2013, 2027)
    uni = {y: nd.heavy(y, 12) for y in years}
    ref = wf.sim(uni.get, 3000, 30000, cost=0, cmin=0)

    vals, idx, cols = adj.values, adj.index, list(adj.columns)
    start = idx.searchsorted(pd.Timestamp("2013-01-01"))
    state, last = {}, {}
    for k in range(start, len(idx)):
        row = vals[k]
        need = set(uni[idx[k].year]) | set(state.get("positions", {}))
        prices = {c: float(row[n]) for n, c in enumerate(cols) if c in need and row[n] == row[n]}
        hist = {}
        for n, c in enumerate(cols):
            if c not in need:
                continue
            h = vals[max(0, k - 300):k, n]
            h = h[h == h]
            if len(h):
                hist[c] = h.tolist()
        rules.step(state, str(idx[k].date()), prices, hist, uni[idx[k].year], CFG)
        last = prices
    realized = sum(t["pnl"] for t in state["trades"])
    open_mtm = sum(b["shares"] * (last[s] - b["px"]) for s, p in state["positions"].items() for b in p["buys"])
    mine = realized + open_mtm
    ref_total = ref["h1"] + ref["h2"]
    assert abs(mine - ref_total) < 0.005 * abs(ref_total) + 50, (mine, ref_total)
