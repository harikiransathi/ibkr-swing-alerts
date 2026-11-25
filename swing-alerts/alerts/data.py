"""Market data. IB Gateway is the primary source and is opened with readonly=True: the API session cannot place,
modify or cancel orders. Yahoo is the fallback when IB is unreachable or a symbol has no IB data.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("alerts.data")
NY = ZoneInfo("America/New_York")


def _ok(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x)) and float(x) > 0
    except (TypeError, ValueError):
        return False


class IBData:
    name = "IB"

    def __init__(self, host: str, port: int, client_id: int) -> None:
        from ib_insync import IB
        self.ib = IB()
        self.host, self.port, self.client_id = host, port, client_id
        self.contracts: dict[str, object] = {}

    def connect(self) -> None:
        self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=30, readonly=True)
        self.ib.reqMarketDataType(1)

    def connected(self) -> bool:
        return self.ib.isConnected()

    def disconnect(self) -> None:
        try:
            self.ib.disconnect()
        except Exception:  # noqa: BLE001
            pass

    def sleep(self, s: float) -> None:
        self.ib.sleep(s)

    def contract(self, sym: str):
        if sym not in self.contracts:
            from ib_insync import Stock
            c = Stock(sym, "SMART", "USD")
            if not self.ib.qualifyContracts(c):
                raise RuntimeError(f"IB does not know {sym}")
            self.contracts[sym] = c
        return self.contracts[sym]

    def session_close(self, sym: str, today: date) -> datetime | None:
        cd = self.ib.reqContractDetails(self.contract(sym))[0]
        key = today.strftime("%Y%m%d")
        for part in (cd.liquidHours or "").split(";"):
            if part.startswith(key):
                if "CLOSED" in part:
                    return None
                return datetime.strptime(part.split("-")[1], "%Y%m%d:%H%M").replace(tzinfo=NY)
        raise RuntimeError("today not found in IB trading hours")

    def daily_closes(self, sym: str, today: date) -> list[float]:
        bars = self.ib.reqHistoricalData(self.contract(sym), "", "2 Y", "1 day", "TRADES", useRTH=True, formatDate=1)
        out = sorted((b.date, float(b.close)) for b in bars or [] if b.date < today and _ok(b.close))
        if len(out) < 250:
            raise RuntimeError(f"only {len(out)} daily bars")
        return [c for _, c in out]

    def prices(self, syms: list[str]) -> dict[str, float]:
        out = {}
        tickers = self.ib.reqTickers(*[self.contract(s) for s in syms])
        for s, t in zip(syms, tickers):
            p = t.last if _ok(t.last) else t.marketPrice()
            if _ok(p) and getattr(t, "marketDataType", 1) == 1:
                out[s] = float(p)
        return out


class YahooData:
    name = "Yahoo"

    def session_close(self, sym: str, today: date) -> datetime | None:
        """16:00 if Yahoo already has a bar for today (half days are not detected), else closed."""
        import yfinance as yf
        if today.weekday() >= 5:
            return None
        df = yf.download(sym, period="5d", interval="1d", auto_adjust=False, progress=False)
        if not len(df) or df.index[-1].date() != today:
            return None
        return datetime.combine(today, datetime.min.time()).replace(hour=16, tzinfo=NY)

    def daily_closes(self, sym: str, today: date) -> list[float]:
        import yfinance as yf
        df = yf.download(sym, period="2y", auto_adjust=False, progress=False)
        s = df["Close"].squeeze().dropna()
        s = s[[d.date() < today for d in s.index]]
        if len(s) < 250:
            raise RuntimeError(f"only {len(s)} daily bars from Yahoo")
        return [float(x) for x in s.values]

    def prices(self, syms: list[str]) -> dict[str, float]:
        import yfinance as yf
        out = {}
        for s in syms:
            try:
                p = yf.Ticker(s).fast_info["last_price"]
                if _ok(p):
                    out[s] = float(p)
            except Exception as ex:  # noqa: BLE001
                log.warning("Yahoo price %s failed: %s", s, ex)
        return out
