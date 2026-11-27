"""Scanner end to end against a fake data source: alerts, paper book, restart safety, holidays, cap skips."""
import json
from datetime import datetime

import pytest

import alerts.scanner as S

NY = S.NY


class FakeSrc:
    name = "IB"

    def __init__(self, hist, px, closed=False):
        self.h, self.px, self.closed = hist, px, closed

    def session_close(self, sym, today):
        return None if self.closed else datetime(today.year, today.month, today.day, 16, 0, tzinfo=NY)

    def daily_closes(self, sym, today):
        return list(self.h[sym])

    def prices(self, syms):
        return {s: self.px[s] for s in syms if s in self.px}

    def connected(self):
        return False

    def disconnect(self):
        pass


CFG = {"alerts_only": True, "ib": {"host": "x", "port": 1, "client_id": 1},
       "schedule": {"decide_minutes_before_close": 5, "preview_minutes_before_decide": 5},
       "rules": {"buy_usd": 3000, "cap_usd": 6000, "max_buys": 3, "max_days": 5}, "sanity_max_move": 0.25,
       "universe": {"mode": "fixed", "symbols": ["AAA", "BBB", "CCC"]}, "files": {}}

UP = [100 + i * 0.5 for i in range(300)]          # steady riser
DIP = UP[:-3] + [UP[-3] * 0.97, UP[-3] * 0.94, UP[-3] * 0.91]


def make(tmp_path, monkeypatch, src, day="2026-11-24"):
    cfg = json.loads(json.dumps(CFG)); cfg["files"]["state_dir"] = str(tmp_path)
    sent = []
    monkeypatch.setattr(S, "now", lambda: datetime.fromisoformat(day + "T15:56:00").replace(tzinfo=NY))
    sc = S.Scanner(cfg, dry_run=True)
    sc.send = sent.append
    monkeypatch.setattr(sc, "connect", lambda give_up: setattr(sc, "src", src))
    monkeypatch.setattr(S, "YahooData", lambda: src)
    return sc, sent


def test_buy_skip_and_no_repeat(tmp_path, monkeypatch):
    src = FakeSrc({"AAA": DIP, "BBB": DIP, "CCC": DIP}, {"AAA": DIP[-1] * 0.97, "BBB": DIP[-1] * 0.96,
                                                        "CCC": DIP[-1] * 0.98})
    sc, sent = make(tmp_path, monkeypatch, src)
    sc.run(immediate=True)
    text = "\n".join(sent)
    assert sum(m.startswith("BUY ") for m in sent) == 2 and "SKIPPED BUY CCC" in text          # cap 6000 = 2 buys, lowest RSI first
    assert set(sc.state["positions"]) == {"AAA", "BBB"}
    sc2, sent2 = make(tmp_path, monkeypatch, src)                       # restart same day: nothing resent
    sc2.run(immediate=True)
    assert not sent2 and sc2.state["positions"].keys() == {"AAA", "BBB"}


def test_sell_next_up_day(tmp_path, monkeypatch):
    src = FakeSrc({"AAA": DIP, "BBB": UP, "CCC": UP}, {"AAA": DIP[-1] * 0.97, "BBB": UP[-1] + 1, "CCC": UP[-1] + 1})
    sc, _ = make(tmp_path, monkeypatch, src)
    sc.run(immediate=True)
    buy = sc.state["positions"]["AAA"]["buys"][0]["px"]
    src2 = FakeSrc({"AAA": DIP + [buy], "BBB": UP, "CCC": UP}, {"AAA": buy * 1.02, "BBB": UP[-1], "CCC": UP[-1]})
    sc2, sent = make(tmp_path, monkeypatch, src2, day="2026-11-25")
    sc2.run(immediate=True)
    msg = next(m for m in sent if m.startswith("SELL AAA"))
    assert "+2.00%" in msg and "up day" in msg and not sc2.state["positions"]
    assert sc2.state["trades"][0]["pnl"] == pytest.approx(60, abs=0.01)


def test_resend_after_crash_and_holiday(tmp_path, monkeypatch):
    src = FakeSrc({"AAA": DIP, "BBB": UP, "CCC": UP}, {"AAA": DIP[-1] * 0.97, "BBB": UP[-1], "CCC": UP[-1]})
    sc, _ = make(tmp_path, monkeypatch, src)
    monkeypatch.setattr(sc, "announce", lambda *a: (_ for _ in ()).throw(RuntimeError("telegram died")))
    with pytest.raises(RuntimeError):
        sc.run(immediate=True)
    sc2, sent = make(tmp_path, monkeypatch, src)
    sc2.run(immediate=True)
    assert any("resending" in m for m in sent) and any(m.startswith("BUY AAA") for m in sent)
    sc3, sent3 = make(tmp_path, monkeypatch, FakeSrc({}, {}, closed=True), day="2026-11-26")
    sc3.run(immediate=True)
    assert not sent3 and "2026-11-26" not in sc3.state["decisions"]


def test_ibs_filter_and_rsi50_exit():
    from alerts import rules
    cfg = {"buy_usd": 3000, "cap_usd": 30000, "max_buys": 3, "max_days": 7,
           "ibs_max": 0.25, "exit_mode": "rsi2_gt_50", "exit_rsi": 50, "min_breadth": 1}
    p_dip = DIP[-1] * 0.97
    # AAA closes near bottom of day range (IBS = 0.10 < 0.25) -> BUY
    # BBB closes near top of day range (IBS = 0.80 >= 0.25) -> filtered out
    hl = {"AAA": (p_dip + 9.0, p_dip - 1.0), "BBB": (p_dip + 2.0, p_dip - 8.0)}
    state = {}
    ev = rules.step(state, "2026-11-24", {"AAA": p_dip, "BBB": p_dip}, {"AAA": DIP, "BBB": DIP}, ["AAA", "BBB"], cfg, day_hl=hl)
    assert [e["sym"] for e in ev if e["type"] == "buy"] == ["AAA"]
    assert ev[0]["ibs"] == pytest.approx(0.10, abs=0.01)
    assert ev[0]["breadth"] == (2, 2)
    # Next day tiny bounce (+0.1%): up day, but RSI(2) is still <= 50 -> must HOLD under rsi2_gt_50!
    ev2 = rules.step(state, "2026-11-25", {"AAA": p_dip * 1.001}, {"AAA": DIP + [p_dip]}, ["AAA"], cfg)
    assert not any(e["type"] == "sell" for e in ev2) and "AAA" in state["positions"]
    # Strong bounce (+5%) -> RSI(2) > 50 -> SELL!
    ev3 = rules.step(state, "2026-11-26", {"AAA": p_dip * 1.05}, {"AAA": DIP + [p_dip, p_dip * 1.001]}, ["AAA"], cfg)
    assert any(e["type"] == "sell" and "RSI(2)" in e["why"] for e in ev3) and "AAA" not in state["positions"]
