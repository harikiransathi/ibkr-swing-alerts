"""Universe = the `top` largest stocks of the pool by market cap at the prior year end, picked once per calendar year.

Market cap = split-adjusted close on the last trading day of the prior year x latest shares outstanding (Yahoo),
the same approximation as the research (ignores buybacks). The pick is cached in state/universe_<year>.json, so it is
computed once in January and then frozen for the year.
    python -m alerts.universe            # show (and cache) this year's list
    python -m alerts.universe 2027       # preview next year's list from data so far
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("alerts.universe")


def compute(pool: list[str], top: int, year: int) -> list[str]:
    import yfinance as yf
    px = yf.download(pool, start=f"{year - 1}-11-01", end=f"{year}-01-01", auto_adjust=False, progress=False)["Close"]
    last = px.ffill().iloc[-1]
    cap = {}
    for t in pool:
        try:
            sh = yf.Ticker(t).get_shares_full(start=f"{year - 2}-01-01")
            if sh is not None and len(sh) and last.get(t) == last.get(t):
                cap[t] = float(last[t]) * float(sh.iloc[-1])
        except Exception as ex:  # noqa: BLE001
            log.warning("no shares for %s: %s", t, ex)
    if len(cap) < 0.8 * len(pool):
        raise RuntimeError(f"market cap available for only {len(cap)}/{len(pool)} pool stocks")
    return [t for t, _ in sorted(cap.items(), key=lambda kv: -kv[1])[:top]]


def for_year(cfg: dict, state_dir: Path, year: int | None = None) -> tuple[list[str], str]:
    """(symbols, note). Fixed list if configured; else cached/computed auto pick; else the configured fallback."""
    u = cfg["universe"]
    if u.get("mode") == "fixed":
        return list(u["symbols"]), "fixed list from config"
    year = year or date.today().year
    f = state_dir / f"universe_{year}.json"
    if f.exists():
        return json.loads(f.read_text())["symbols"], f"top {u['top']} by market cap, picked {f.name}"
    try:
        syms = compute(list(u["pool"]), int(u["top"]), year)
    except Exception as ex:  # noqa: BLE001
        return list(u["fallback"]), f"WARNING: auto pick failed ({ex}); using the config fallback list"
    if year <= date.today().year:
        state_dir.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"year": year, "symbols": syms, "pool": u["pool"], "top": u["top"]}, indent=1))
    return syms, f"top {u['top']} by market cap at {year - 1} year end (new pick)"


if __name__ == "__main__":
    import yaml
    cfg = yaml.safe_load((ROOT / "config" / "alerts.yaml").read_text())
    y = int(sys.argv[1]) if len(sys.argv) > 1 else None
    print(*for_year(cfg, ROOT / cfg["files"]["state_dir"], y), sep="\n")
