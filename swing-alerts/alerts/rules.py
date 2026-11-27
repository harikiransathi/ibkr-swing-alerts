"""Stock swing rules (research: pivot-live/tools/stock_swing_wf.py and stock_swing_ndx.py).

Decided once a day at ~15:55 ET on price p, with `hist` = completed daily closes up to yesterday:
  first buy : RSI(2) of hist+[p] < 10
  add (2nd/3rd buy): already held, fewer than max_buys, p < previous buy price - ATR, ATR = mean |close change| over
                     the last 14 changes of hist+[p]
  sell all  : p > yesterday's close, or the position has been held `max_days` trading days
  capital   : buy_usd per buy, at most cap_usd open; when full, lowest RSI(2) first, the rest are skipped
Sells are processed before buys, so a stock sold today can be bought again the same day (as in the backtest).
"""

from __future__ import annotations

import math

import pandas as pd


def rsi2(closes: list[float]) -> float:
    d = pd.Series(closes, dtype=float).diff()
    up = d.clip(lower=0).ewm(alpha=0.5).mean().iloc[-1]
    dn = (-d).clip(lower=0).ewm(alpha=0.5).mean().iloc[-1]
    return float(100 - 100 / (1 + up / (dn if dn > 0 else 1e-9)))


def atr(closes: list[float], n: int = 14) -> float:
    s = pd.Series(closes[-(n + 1):], dtype=float)
    return float(s.diff().abs().mean())


def ibs_val(price: float, hl: tuple[float, float] | None) -> float | None:
    if hl is None:
        return None
    hi, lo = hl
    if hi <= lo:
        return None
    return float((price - lo) / (hi - lo))


def step(state: dict, day: str, prices: dict[str, float], hist: dict[str, list[float]], universe: list[str],
         cfg: dict, day_hl: dict[str, tuple[float, float]] | None = None) -> list[dict]:
    """Apply one decision day to `state` (mutated) and return the events (sell / buy / add / skip)."""
    size, cap, max_buys, max_days = (float(cfg["buy_usd"]), float(cfg["cap_usd"]), int(cfg["max_buys"]),
                                     int(cfg["max_days"]))
    exit_mode = str(cfg.get("exit_mode", "up_day"))
    exit_rsi = float(cfg.get("exit_rsi", 50.0))
    ibs_max = cfg.get("ibs_max")
    min_breadth = int(cfg.get("min_breadth", 1))
    hl_map = day_hl or {}
    pos, ev = state.setdefault("positions", {}), []
    for sym in sorted(pos):
        p, h = prices.get(sym), hist.get(sym) or []
        if p is None or not h:
            ev.append({"type": "stale", "sym": sym, "why": "no price today; position kept, day not counted"})
            continue
        ps = pos[sym]
        ps["days"] += 1
        r_now = rsi2(h + [p])
        if exit_mode == "rsi2_gt_50":
            sig_exit = math.isfinite(r_now) and r_now > exit_rsi
            exit_why = f"RSI(2) {r_now:.1f} > {exit_rsi:g}"
        else:
            sig_exit = p > h[-1]
            exit_why = f"up day (> {h[-1]:.2f})"
        timed = ps["days"] >= max_days
        if sig_exit or timed:
            cost = sum(b["shares"] * b["px"] for b in ps["buys"])
            value = sum(b["shares"] for b in ps["buys"]) * p
            tr = {"sym": sym, "entry_date": ps["buys"][0]["date"], "exit_date": day, "buys": len(ps["buys"]),
                  "avg_entry": round(cost / sum(b["shares"] for b in ps["buys"]), 4), "exit": p,
                  "pct": round((value / cost - 1) * 100, 3), "pnl": round(value - cost, 2), "days": ps["days"],
                  "why": exit_why if sig_exit else f"day {ps['days']} time limit"}
            state.setdefault("trades", []).append(tr)
            ev.append({"type": "sell", **tr, "legs": ps["buys"], "prev_close": h[-1]})
            del pos[sym]
    used = sum(len(p["buys"]) for p in pos.values()) * size
    # Count breadth across the universe (how many stocks have RSI(2) < 10 today)
    rsi_map = {}
    for sym in universe:
        p, h = prices.get(sym), hist.get(sym) or []
        if p is not None and len(h) >= 30:
            r = rsi2(h + [p])
            if math.isfinite(r):
                rsi_map[sym] = r
    n_oversold = sum(1 for r in rsi_map.values() if r < 10)
    cands = []
    for sym in universe:
        p, h = prices.get(sym), hist.get(sym) or []
        if p is None or len(h) < 30 or sym not in rsi_map:
            continue
        r, a = rsi_map[sym], atr(h + [p])
        if not math.isfinite(a):
            continue
        iv = ibs_val(p, hl_map.get(sym))
        if sym in pos:
            last = pos[sym]["buys"][-1]["px"]
            if len(pos[sym]["buys"]) < max_buys and p < last - a:
                cands.append((r, sym, "add", p, a, last, iv))
        elif r < 10 and n_oversold >= min_breadth:
            if ibs_max is not None and iv is not None and iv >= float(ibs_max):
                continue
            cands.append((r, sym, "buy", p, a, None, iv))
    for r, sym, kind, p, a, last, iv in sorted(cands):
        if used + size > cap + 1e-6:
            ev.append({"type": "skip", "sym": sym, "kind": kind, "px": p, "rsi2": round(r, 2),
                       "why": f"capital cap ${cap:,.0f} full"})
            continue
        used += size
        leg = {"date": day, "px": p, "shares": size / p}
        if kind == "add":
            pos[sym]["buys"].append(leg)
        else:
            pos[sym] = {"buys": [leg], "days": 0}
        ev.append({"type": kind, "sym": sym, "px": p, "rsi2": round(r, 2), "atr": round(a, 2), "prev_buy": last,
                   "n": len(pos[sym]["buys"]), "max": max_buys, "used": used,
                   "ibs": round(iv, 2) if iv is not None else None,
                   "breadth": (n_oversold, len(universe))})
    return ev


def preview(state: dict, prices: dict[str, float], hist: dict[str, list[float]], universe: list[str],
            cfg: dict, day_hl: dict[str, tuple[float, float]] | None = None) -> list[str]:
    """Plain-language 'what happens at 3:55 if the price stays here' lines."""
    out = []
    exit_mode = str(cfg.get("exit_mode", "up_day"))
    exit_rsi = float(cfg.get("exit_rsi", 50.0))
    ibs_max = cfg.get("ibs_max")
    hl_map = day_hl or {}
    for sym, ps in sorted(state.get("positions", {}).items()):
        p, h = prices.get(sym), hist.get(sym) or []
        if p is None or not h:
            continue
        day = ps["days"] + 1
        r_now = rsi2(h + [p])
        if exit_mode == "rsi2_gt_50":
            if math.isfinite(r_now) and r_now > exit_rsi:
                out.append(f"{sym}: held, RSI(2) {r_now:.1f} > {exit_rsi:g} -> SELL at 3:55 if it holds")
            elif day >= int(cfg["max_days"]):
                out.append(f"{sym}: held, day {day} -> SELL at 3:55 (time limit)")
            else:
                a = atr(h + [p])
                nb = len(ps["buys"])
                add = (f", add #{nb + 1} if below {ps['buys'][-1]['px'] - a:.2f}" if nb < int(cfg["max_buys"]) else "")
                out.append(f"{sym}: held day {day}, RSI(2) {r_now:.1f} -> hold{add}")
        else:
            if p > h[-1]:
                out.append(f"{sym}: held, now {p:.2f} > yesterday {h[-1]:.2f} -> SELL at 3:55 if it stays up")
            elif day >= int(cfg["max_days"]):
                out.append(f"{sym}: held, day {day} -> SELL at 3:55 (time limit)")
            else:
                a = atr(h + [p])
                nb = len(ps["buys"])
                add = (f", add #{nb + 1} if below {ps['buys'][-1]['px'] - a:.2f}" if nb < int(cfg["max_buys"]) else "")
                out.append(f"{sym}: held day {day}, now {p:.2f} vs yesterday {h[-1]:.2f} -> hold{add}")
    for sym in universe:
        p, h = prices.get(sym), hist.get(sym) or []
        if sym in state.get("positions", {}) or p is None or len(h) < 30:
            continue
        r = rsi2(h + [p])
        if r < 20:
            iv = ibs_val(p, hl_map.get(sym))
            ibs_ok = (ibs_max is None or iv is None or iv < float(ibs_max))
            ibs_txt = f", IBS {iv:.2f}" if iv is not None else ""
            out.append(f"{sym}: RSI(2) {r:.1f}{ibs_txt} at {p:.2f} -> {'BUY at 3:55 if it stays' if (r < 10 and ibs_ok) else 'close to a buy'}")
    return out
