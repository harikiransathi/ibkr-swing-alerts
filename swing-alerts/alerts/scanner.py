"""Swing alerts: one run per trading day, Telegram alerts only. NEVER places orders (IB is opened read-only and this
package contains no order code; tests/test_guardrails.py checks both).

    python -m alerts.scanner               # normal run (scheduled 15:40 ET): preview 15:50, decide 15:55, exit
    python -m alerts.scanner --now         # decide immediately on current prices (testing; real state + alerts)
    python -m alerts.scanner --dry-run     # like a normal run but no Telegram and a separate dry state file
    python -m alerts.scanner --status      # print paper positions and trades, no data needed
    run_alerts.bat                         # what the scheduled task runs

Paper book: every BUY alert is assumed filled at the alert price for buy_usd (stock-equivalent), and every SELL alert
closes it at the alert price. P/L in alerts is that stock-equivalent paper result, not your LEAPS result.
Restarts are safe: a day's decision is stored in the state before any alert goes out and is never made twice.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alerts import rules, universe  # noqa: E402
from alerts.data import IBData, YahooData  # noqa: E402
from alerts.notify import Telegram  # noqa: E402

NY = ZoneInfo("America/New_York")
log = logging.getLogger("alerts.scanner")


def now() -> datetime:
    return datetime.now(NY)


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


class Scanner:
    def __init__(self, cfg: dict, dry_run: bool) -> None:
        if not cfg.get("alerts_only", False):
            raise SystemExit("config alerts_only must be true: this program only sends alerts")
        self.cfg, self.r, self.dry_run = cfg, cfg["rules"], dry_run
        self.state_dir = ROOT / cfg["files"]["state_dir"]
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / ("alerts_dry.json" if dry_run else "alerts.json")
        self.today = now().date()
        self.tg = Telegram(prefix="[STOCK LEAPS ALERT] ") if not dry_run else Telegram.__new__(Telegram)
        if dry_run:
            self.tg.enabled, self.tg.prefix = False, ""
        self.state = self.load_state()
        self.src = None
        self.hist: dict[str, list[float]] = {}
        self.syms: list[str] = []

    # ---- state ---------------------------------------------------------------------------------------------
    def load_state(self) -> dict:
        try:
            s = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            s = {}
        except ValueError as ex:
            raise SystemExit(f"{self.state_file} is corrupt ({ex}); restore it from {self.state_file.name}.bak") from None
        for k, v in (("positions", {}), ("trades", []), ("decisions", {})):
            s.setdefault(k, v)
        return s

    def save(self) -> None:
        if self.state_file.exists():
            atomic_write(self.state_file.with_suffix(".json.bak"), self.state_file.read_text(encoding="utf-8"))
        self.state["updated"] = now().isoformat(timespec="seconds")
        atomic_write(self.state_file, json.dumps(self.state, indent=1, default=str))

    def send(self, text: str) -> None:
        print(text, flush=True) if self.dry_run else None
        self.tg.send(text)

    # ---- data ----------------------------------------------------------------------------------------------
    def connect(self, give_up: datetime) -> None:
        ib = self.cfg["ib"]
        src, tries, last = IBData(ib["host"], int(ib["port"]), int(ib["client_id"])), 0, -1e9
        while True:
            try:
                src.connect()
                self.src = src
                if tries:
                    self.send(f"OK: connected to IB Gateway after {tries} failed tries")
                return
            except Exception as ex:  # noqa: BLE001
                tries += 1
                src.disconnect()
                log.error("IB connect failed (try %d): %s", tries, ex)
                if now() >= give_up:
                    self.src = YahooData()
                    self.send(f"WARNING: IB Gateway unreachable on port {ib['port']}; using Yahoo prices today. "
                              "Double-check prices before acting.")
                    return
                if tries == 1 or time.monotonic() - last >= 600:
                    self.send(f"WARNING: cannot connect to IB Gateway on port {ib['port']} ({ex or type(ex).__name__});"
                              " retrying every minute, Yahoo fallback at "
                              f"{give_up:%H:%M}")
                    last = time.monotonic()
                time.sleep(60)

    def load_history(self) -> None:
        yahoo = YahooData()
        need = sorted(set(self.syms) | set(self.state["positions"]))
        for s in need:
            for src in ([self.src, yahoo] if self.src.name == "IB" else [yahoo]):
                try:
                    self.hist[s] = src.daily_closes(s, self.today)
                    log.info("%s: %d closes from %s, last %.2f", s, len(self.hist[s]), src.name, self.hist[s][-1])
                    break
                except Exception as ex:  # noqa: BLE001
                    log.warning("%s history from %s failed: %s", s, src.name, ex)
            else:
                self.send(f"WARNING: no daily history for {s}; it is skipped today")

    def prices(self) -> tuple[dict[str, float], str]:
        need = sorted(self.hist)
        got, note = {}, ""
        if self.src.name == "IB":
            try:
                got = self.src.prices(need)
            except Exception as ex:  # noqa: BLE001
                log.warning("IB prices failed: %s", ex)
        miss = [s for s in need if s not in got]
        if miss:
            y = YahooData().prices(miss)
            got.update(y)
            if y:
                note = f" (Yahoo price for {', '.join(sorted(y))})"
        for s, p in got.items():
            h = self.hist.get(s) or []
            if h and abs(p / h[-1] - 1) > float(self.cfg["sanity_max_move"]):
                self.send(f"WARNING: {s} price {p:.2f} is {100 * (p / h[-1] - 1):+.0f}% vs yesterday {h[-1]:.2f}; "
                          "check for a split or bad tick. Using it anyway.")
        return got, note

    def day_hl(self, syms: list[str]) -> dict[str, tuple[float, float]]:
        hl: dict[str, tuple[float, float]] = {}
        if self.src and hasattr(self.src, "day_hl"):
            try:
                hl = self.src.day_hl(syms)
            except Exception as ex:  # noqa: BLE001
                log.warning("IB day_hl failed: %s", ex)
        miss = [s for s in syms if s not in hl]
        if miss and self.src and self.src.name in ("IB", "Yahoo"):
            try:
                hl.update(YahooData().day_hl(miss))
            except Exception as ex:  # noqa: BLE001
                log.warning("Yahoo day_hl failed: %s", ex)
        return hl

    def wait_until(self, t: datetime) -> None:
        while now() < t:
            left = (t - now()).total_seconds()
            if self.src and self.src.name == "IB" and self.src.connected():
                self.src.sleep(min(left, 5))
            else:
                time.sleep(min(max(left, 0.1), 5))

    # ---- messages ------------------------------------------------------------------------------------------
    def fmt(self, e: dict) -> str:
        size = float(self.r["buy_usd"])
        exit_mode = str(self.r.get("exit_mode", "up_day"))
        sell_desc = (f"RSI(2) > {self.r.get('exit_rsi', 50):g}, or day {self.r['max_days']}"
                     if exit_mode == "rsi2_gt_50"
                     else f"first close above the previous close, or day {self.r['max_days']}")
        if e["type"] in ("buy", "add"):
            what = "BUY" if e["type"] == "buy" else f"ADD (buy {e['n']} of {e['max']})"
            if e["type"] == "buy":
                ibs_txt = f", IBS {e['ibs']:.2f}" if e.get("ibs") is not None else ""
                br = e.get("breadth")
                br_txt = f", breadth {br[0]}/{br[1]} oversold" if br else ""
                extra = f"RSI(2) {e['rsi2']:.1f} < 10{ibs_txt}{br_txt}"
            else:
                extra = f"price is more than 1 ATR ({e['atr']:.2f}) below the last buy {e['prev_buy']:.2f}"
            return (f"{what} {e['sym']} @ {e['px']:.2f}\n{extra}. Paper size ${size:,.0f}. "
                    f"Sell rule: {sell_desc}.")
        if e["type"] == "sell":
            legs = ", ".join(f"{b['px']:.2f} ({b['date'][5:]})" for b in e["legs"])
            return (f"SELL {e['sym']} @ {e['exit']:.2f} - all {e['buys']} buy(s): {legs}\nReason: {e['why']}. "
                    f"Paper result {e['pct']:+.2f}% = ${e['pnl']:+,.0f} on ${size * e['buys']:,.0f}.")
        if e["type"] == "skip":
            return f"SKIPPED {e['kind'].upper()} {e['sym']} @ {e['px']:.2f} (RSI(2) {e['rsi2']:.1f}): {e['why']}"
        return f"NOTE {e['sym']}: {e['why']}"

    def summary(self) -> str:
        pos, size = self.state["positions"], float(self.r["buy_usd"])
        lines = [f"Open: {len(pos)} stocks, ${sum(len(p['buys']) for p in pos.values()) * size:,.0f} of "
                 f"${float(self.r['cap_usd']):,.0f}"]
        for s, p in sorted(pos.items()):
            lines.append(f"  {s}: {len(p['buys'])} buy(s) avg "
                         f"{sum(b['px'] for b in p['buys']) / len(p['buys']):.2f}, day {p['days']}")
        tr = self.state["trades"]
        ytd = [t for t in tr if t["exit_date"][:4] == str(self.today.year)]
        if tr:
            lines.append(f"Closed: {len(tr)} trades, ${sum(t['pnl'] for t in tr):+,.0f} total, "
                         f"{self.today.year} ${sum(t['pnl'] for t in ytd):+,.0f}, "
                         f"win {sum(t['pnl'] > 0 for t in tr) / len(tr):.0%}")
        return "\n".join(lines)

    # ---- run -----------------------------------------------------------------------------------------------
    def run(self, immediate: bool) -> None:
        key = self.today.isoformat()
        if key in self.state["decisions"]:
            d = self.state["decisions"][key]
            if not d.get("sent"):
                self.send(f"(resending alerts of the {d['at'][11:16]} decision after a restart)")
                self.announce(key, d["events"], d["source"])
            log.info("decision for %s already made at %s; nothing to do", key, d["at"])
            return
        start = now()
        self.connect(give_up=start.replace(hour=15, minute=50, second=0) if not immediate else start)
        close = self.src.session_close(self.cfg["universe"].get("calendar_symbol", "AAPL"), self.today)
        if close is None:
            log.info("market closed today")
            return
        decide = close - timedelta(minutes=int(self.cfg["schedule"]["decide_minutes_before_close"]))
        preview_at = decide - timedelta(minutes=int(self.cfg["schedule"]["preview_minutes_before_decide"]))
        if not immediate and now() > decide + timedelta(minutes=3):
            self.send(f"WARNING: started at {now():%H:%M}, after the {decide:%H:%M} decision time; no decision today")
            return
        self.syms, note = universe.for_year(self.cfg, self.state_dir, self.today.year)
        if note.startswith("WARNING"):
            self.send(note)
        self.load_history()
        if not immediate and now() < preview_at:
            self.wait_until(preview_at)
        if not immediate:
            pr, _ = self.prices()
            hl = self.day_hl(sorted(pr)) if self.r.get("ibs_max") is not None else {}
            lines = rules.preview(self.state, pr, self.hist, self.syms, self.r, day_hl=hl)
            self.send(f"Preview {now():%H:%M} (decision at {decide:%H:%M}):\n" +
                      ("\n".join(lines) if lines else "nothing close to a signal"))
            self.wait_until(decide)
        pr, pnote = self.prices()
        hl = self.day_hl(sorted(pr)) if self.r.get("ibs_max") is not None else {}
        for s in self.state["positions"]:
            if s not in pr:
                self.send(f"WARNING: no price for held {s}; it is not checked for a sell today")
        events = rules.step(self.state, key, pr, self.hist, self.syms, self.r, day_hl=hl)
        self.state["decisions"][key] = {"at": now().isoformat(timespec="seconds"), "source": self.src.name + pnote,
                                        "universe": self.syms, "events": events, "sent": False}
        self.save()
        self.announce(key, events, self.src.name + pnote)
        if not immediate:
            self.remind_buys(events, close)

    def remind_buys(self, events: list[dict], close: datetime) -> None:
        """Repeat a concise Telegram reminder every N seconds until market close when BUY/ADD candidates exist."""
        every = int(self.cfg.get("schedule", {}).get("remind_until_close_seconds", 60))
        buys = [e for e in events if e.get("type") in ("buy", "add")]
        if every <= 0 or not buys:
            return
        items = ", ".join(f"{e['type'].upper()} {e['sym']} @ {e['px']:.2f}" for e in buys)
        while True:
            nxt = now() + timedelta(seconds=every)
            if nxt >= close:
                return
            self.wait_until(nxt)
            left_m = max(1, int(round((close - now()).total_seconds() / 60)))
            self.send(f"REMINDER ({now():%H:%M}, {left_m}m to close): {items}")
            self.tg.flush()

    def announce(self, key: str, events: list[dict], source: str) -> None:
        order = ("sell", "buy", "add", "skip", "stale")
        for e in sorted((e for e in events if e["type"] in order), key=lambda e: order.index(e["type"])):
            self.send(self.fmt(e))
        self.send(f"{key} decision at {self.state['decisions'][key]['at'][11:16]} on {source} prices: "
                  f"{sum(e['type'] == 'sell' for e in events)} sell, "
                  f"{sum(e['type'] in ('buy', 'add') for e in events)} buy\n" + self.summary())
        self.tg.flush()
        self.state["decisions"][key]["sent"] = True
        self.save()


def setup_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(ROOT / "logs" / f"alerts_{date.today()}.log", encoding="utf-8"),
                                  logging.StreamHandler()])
    logging.getLogger("ib_insync").setLevel(logging.WARNING)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    setup_logging()
    cfg = yaml.safe_load((ROOT / "config" / "alerts.yaml").read_text(encoding="utf-8"))
    sc = Scanner(cfg, a.dry_run)
    if a.status:
        print(sc.summary())
        for t in sc.state["trades"][-10:]:
            print(f"  {t['sym']} {t['entry_date']} -> {t['exit_date']} {t['buys']} buy(s) {t['pct']:+.2f}% ${t['pnl']:+,.0f}")
        return
    try:
        sc.run(a.now)
    except SystemExit:
        raise
    except Exception as ex:  # noqa: BLE001
        log.error("crash: %s", traceback.format_exc())
        sc.tg.send(f"CRITICAL: swing alerts crashed ({type(ex).__name__}: {ex}). Rerun run_alerts.bat; "
                   "a decision already made today will not be repeated.")
    finally:
        if sc.src and sc.src.name == "IB":
            sc.src.disconnect()
        if sc.tg.enabled:
            sc.tg.flush()


if __name__ == "__main__":
    main()
