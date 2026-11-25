# Swing alerts

Telegram alerts for the stock swing rule. **It never places orders**: IB Gateway is opened read-only and the code
contains no order calls (`tests/test_guardrails.py` checks both). You execute; the program keeps a paper book that
assumes each BUY alert filled at the alert price and each SELL alert closed it.

## Rules (from `pivot-live/tools/stock_swing_ndx.py`, "heavy 12")
- Universe: the 12 largest stocks of a 40-stock Nasdaq-100 pool by market cap at the prior year end, picked once in
  January (`state/universe_<year>.json`). Check next year's list with `python -m alerts.universe 2027`.
- BUY at 15:55 ET when RSI(2) (on prior closes plus the 15:55 price) < 10.
- ADD (2nd and 3rd buy) when already held and the price is at least 1 ATR (14-day mean absolute close change) below
  the previous buy.
- SELL everything on the first day the 15:55 price is above the previous close, or on day 5.
- Paper size $3,000 per buy, $30,000 cap; when full, lowest RSI(2) first and the rest are sent as SKIPPED.

Backtest 2013-2026 (shares, $3k per buy, $30k cap): about $2,400/year, max drawdown about -$3,600, 2 losing years.
These are stock-equivalent numbers; LEAPS results will differ (spread, IV changes, leverage).

## Daily operation
1. IB Gateway running (port 4001; read-only use, client id 99). Without it the scanner falls back to Yahoo prices
   and says so in a WARNING.
2. Windows Task Scheduler: `run_alerts.bat` at 15:40 ET on weekdays.
3. Telegram: 15:50 preview (held positions, near signals), 15:55 BUY / ADD / SELL / SKIPPED alerts and a summary.
   Holidays: nothing is sent. Half days: decision at 12:55 (IB calendar).
4. `run_alerts.bat --status` shows the paper positions and recent trades.

Restarts are safe: the day's decision is saved before alerts go out, never made twice, and unsent alerts are resent
on the next start. `state/alerts.json` (paper book) has a `.bak` copy from the previous save.

## Setup
```
pip install -r requirements.txt
copy config\secrets.example.yaml config\secrets.yaml   (or set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)
python -m pytest -q        (test_parity.py replays 2013-2026 against the research backtest, ~3 min)
run_alerts.bat --dry-run   (prints instead of sending, separate state file)
```
