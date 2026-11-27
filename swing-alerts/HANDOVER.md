# Swing alerts - handover

Last updated: 2026-11-27.

## 1. What this is
This is a daily scanner. At 15:55 ET it sends Telegram alerts to buy, add to or sell large-cap Nasdaq stocks, using a
short-term "buy exhausted dips (`RSI(2) < 10` + `IBS < 0.25`), sell when momentum recovers (`RSI(2) > 50` or day 7)" rule.

**It never places orders.** The owner executes the trades by hand, currently with LEAPS calls. The scanner keeps a
paper book in stock-equivalent dollars. It assumes each alert filled at the alert price.

It is a separate project from `pivot-live`. That project trades MNQ/MES futures automatically; it shares nothing with
this one at runtime except the IB Gateway, which this scanner uses with its own client id.

## 2. Guardrails
| Guardrail | How it is enforced |
|---|---|
| No orders, ever | IB is opened with `readonly=True` (`alerts/data.py`), and the package contains no order calls. `tests/test_guardrails.py` checks both. |
| Alert-only config | `alerts_only: true` must be set in `config/alerts.yaml`; otherwise the scanner exits at start. |
| Secrets | Telegram credentials come only from env vars `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` or from `config/secrets.yaml`. That file, `state/` and `logs/` are git-ignored. |
| No clash with pivot-live | IB client id 99; pivot-live uses 97 (trader) and 98 (swing). |
| Restart safety | The day's decision is saved before any alert goes out, and is never made twice. Unsent alerts are resent on the next start. |
| State backup | Every save first copies `state/alerts.json` to `state/alerts.json.bak`. A corrupt state file stops the scanner, with instructions. |
| Bad data | IB is the main source; Yahoo is the fallback, always announced with a WARNING. Any price more than 25% away from yesterday's close triggers a WARNING. IB quotes that are not real-time are ignored. |
| Workspace rules | Back up before deleting (`.bak`). Commit or push only when the owner asks. No AI signatures in commits. |

## 3. Rules
They come from the research in `pivot-live/tools/`: `stock_swing.py`, `stock_swing_port.py`, `stock_swing_wf.py`,
`stock_swing_ndx.py`, and `stock_swing_oob.py` (upgraded Config B, 2026-11-27).

| Item | Rule |
|---|---|
| Universe | The 12 largest stocks of a 40-stock Nasdaq-100 pool by market cap at the prior year end. They are picked once each January, cached in `state/universe_<year>.json`, and frozen for the year. |
| Decision time | 5 minutes before the close (15:55; 12:55 on half days), on the latest price `p` and today's high/low |
| First buy | `RSI(2) < 10` **and** `IBS = (p - day_low) / (day_high - day_low) < 0.25` (closes in bottom 25% of today's range). Every BUY alert also prints live breadth (`X/12 oversold`; broad dips `>= 3/12` carry almost all of the edge). |
| Add (2nd and 3rd buy) | Already held, fewer than 3 buys, and `p` is at least 1 ATR below the previous buy. ATR = mean absolute close-to-close change over the last 14 changes. |
| Sell (all buys together) | `RSI(2) > 50`, or the position is on its 7th trading day (avg hold ~3.1d, avg gain +1.38% -> clears LEAPS bid-ask spreads; 3.2x better than the old 1-day exit at 40 bps friction). |
| Size and cap | $3,000 paper per buy and $30,000 max open. When the cap is full, signals with the lowest RSI(2) are taken first; the rest are alerted as SKIPPED. |
| Order of processing | Sells first, then buys. A stock sold today can be bought again the same day, as in the backtest. |
| Stop loss | None. The 7-day limit is the only exit for a falling position. |

The 2026 universe is NVDA, AAPL, GOOG, MSFT, AMZN, TSLA, META, AVGO, ASML, NFLX, COST and AMD. Preview next year's
list with `python -m alerts.universe 2027`.

## 4. Evidence and its limits
The backtest uses adjusted daily closes, 2013-2026, with shares, $3k per buy and a $30k cap, and 0.05% plus a $1
minimum per order as costs:

| Universe | Avg per year | Largest drawdown | Losing years | Sharpe |
|---|---:|---:|---:|---:|
| **Largest 12 by market cap (used)** | about $2,400 | about -$3,600 | 2 | 0.98 |
| Top 12 by trailing 3-year rule results | about $3,000 | about -$5,100 | 2 | 0.89 |
| Random 12 | about $2,600 | about -$5,000 | 2 | 0.91 |

- Choosing the largest stocks adds little profit over random picks, but it cuts drawdowns by roughly 30%. It also has
  no tuned parameters, which is why it was chosen.
- On the 49-stock watchlist, RSI(2) < 10 beat random entry days for 71% of stocks. Adding a 200-day-average filter
  made it worse.
- Energy, banks, BA and UPS lost money with this rule. That is why the universe stays in large-cap growth stocks.
- Adds 2 and 3 helped in both halves of the data. The "1 ATR lower" add rule was the best of 9 add rules tested,
  so expect slightly less live.
- The last trading day of each month is not special for this rule. Using the 16:00 close instead of the 15:55 price
  changes the trade count, not the edge per trade.

Known biases:
- **Survivorship:** the pool contains only stocks that still trade today.
- **Market cap approximation:** today's share count is used for past years, ignoring buybacks.
- **Index leverage:** the portfolio is effectively a leveraged bet on broad Nasdaq bounces. Signals bunch up in
  selloffs; on 2026-11-20, 11 positions were open and 6 more signals were skipped.

**LEAPS are not backtested.** No historical option prices were available. Black-Scholes estimates suggest that
spreads and falling implied volatility on the bounce can eat most of the edge on 1-5 day holds. The live test exists
to measure that. The paper P/L in the alerts is the stock-equivalent result, not the LEAPS result.

**Tax:** about 15-20 trades a month in a TFSA risks the CRA treating it as "carrying on a business". The owner was
advised to get professional advice, or to use a taxable account.

## 5. Files
| Path | Purpose |
|---|---|
| `alerts/rules.py` | Pure rules: `rsi2`, `atr`, `step` (one decision day, which updates the paper book) and `preview` (the 15:50 lines) |
| `alerts/scanner.py` | Daily run: connect, universe, history, preview, decision, alerts, summary. Options `--now`, `--dry-run`, `--status`. |
| `alerts/data.py` | `IBData` (read-only) and `YahooData` (fallback) for trading hours, daily closes and prices |
| `alerts/universe.py` | Yearly market-cap pick, with the cache in `state/` and a fallback list in the config |
| `alerts/notify.py` | Telegram sender in a background thread, copied from pivot-live |
| `config/alerts.yaml` | All settings: IB, schedule, rules, universe pool and fallback |
| `config/secrets.example.yaml` | Template for `config/secrets.yaml`, which is git-ignored |
| `state/alerts.json` (+ `.bak`) | Paper book: positions, closed trades, and decisions by date (events, data source, sent flag) |
| `state/alerts_dry.json` | Paper book for `--dry-run` runs, kept apart from the real one |
| `state/universe_<year>.json` | The frozen stock list for that year |
| `logs/alerts_<date>.log` | Run log |
| `run_alerts.bat` | Scheduled entry point (CRLF line endings) |
| `tests/` | Guardrails, parity with the research backtest, and end-to-end scenarios |

## 6. Daily operation
1. On the machine running IB Gateway (port 4001), schedule `run_alerts.bat` in Windows Task Scheduler for weekdays
   at 15:40 ET.
2. Telegram messages arrive in this order:
   - **15:50 preview:** each held stock (hold, sell if it stays up, or the next add level) and stocks close to a buy
     (RSI(2) below 20).
   - **15:55 decisions:** `SELL`, `BUY`, `ADD (buy n of 3)` and `SKIPPED` alerts. Each one includes the price, the
     reason and the paper result.
   - **Summary:** open positions, capital in use, total and year-to-date P/L, and the win rate.
3. The owner places LEAPS orders within about 5 minutes. Use limit orders near the midpoint.
4. `run_alerts.bat --status` shows the paper positions and the last 10 trades.
5. On holidays nothing is sent. On half days the decision is at 12:55, from the IB calendar.

Recommended: log the actual LEAPS fills next to each alert, with date, strike, expiry, fill price and exit price.
After 30 or more round trips, compare them with the paper results.

## 7. Failure handling
| Situation | Behavior |
|---|---|
| IB Gateway down | Retries every minute with a WARNING every 10 minutes. At 15:50 it switches to Yahoo prices and announces that. |
| Started after 15:58 | WARNING, and no decision that day |
| Crash before the decision | CRITICAL alert. Rerun `run_alerts.bat`. |
| Crash after the decision but before alerts went out | The next run resends that decision's alerts ("resending ...") without deciding again |
| No price for a held stock | WARNING. The position is kept, and that day does not count toward the 5-day limit. |
| No history for a stock | WARNING, and the stock is skipped today |
| Universe pick fails in January (Yahoo shares missing) | WARNING, and the config `fallback` list is used. Retried on the next run, because only successful picks are cached. |
| `state/alerts.json` corrupt | The scanner refuses to start. Restore `state/alerts.json.bak`. |

## 8. Testing
```
python -m pytest -q                      # 8 tests; test_parity.py takes about 3 minutes
python -m alerts.scanner --dry-run --now # real data, prints instead of sending, separate state file
```
- `test_guardrails.py`: no order code, read-only IB, refusal to start without `alerts_only`, secrets git-ignored.
- `test_parity.py`: replays `rules.step` day by day over 2013-2026 on the "heavy 12" universe. It must match
  `pivot-live/tools/stock_swing_wf.sim` (without costs) to within 0.5%. It is skipped if the pivot-live research data
  is missing.
- `test_scanner.py`: a fake data source checks the cap and SKIPPED alerts, the next-day sell and its P/L, no repeat
  after a restart, resending after a crash, and silence on holidays.

A November 2026 replay through the real alert formatting gave 23 trades, 87% winners and +$3,002 paper. That included
the NVDA buys at 156.14, 145.98 and 125.53, sold on 11-23 at +16.8%.

## 9. Known gaps and next steps
- Half days are detected only from the IB calendar. In Yahoo-fallback mode the decision assumes a 16:00 close.
- If a stock splits while it's held, the paper entry prices aren't adjusted, and the sanity WARNING fires. Fix the
  paper book by hand if that happens.
- Market cap uses today's share count, so the January pick can differ slightly from the real index weights. Check it
  against the published Nasdaq-100 weights each January.
- Not yet set up: the scheduled task and the Telegram credentials on the trading machine, and a git remote (the owner
  decides).
- After about 30 LEAPS round trips, compare actual fills with the paper results. If spreads and implied volatility
  take most of the edge, switch execution to shares (taxable account) or drop the idea.
