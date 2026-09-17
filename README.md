# Defensive Trading Bot for Trading 212

**v1.0** — 2026-09-18

An algorithmic trading toolkit for Trading 212 focused on **defensive, rule-based
trend and momentum trading**. It combines market-regime analysis, technical
indicators, position sizing, portfolio risk management, and a full buy+sell
dry-run bot — plus the plumbing to pull your live portfolio, screen a
watchlist, and log every (simulated) trade with full indicator context for later
analysis.

## Release notes

### Unreleased

- **Backtesting** (`backtest.py`): runs `trading_bot.py`'s exact
  `evaluate_buy()`/`evaluate_exit()` against historical daily bars, so
  backtest results can't drift from live dry-run behavior — only the data
  source (historical vs live) and portfolio bookkeeping (simulated fills vs
  real T212 state) differ. Writes to an isolated `backtest.db`, wiped and
  rebuilt every run.
- **Performance/accuracy metrics** (`metrics.py`): total return, CAGR, max
  drawdown, Sharpe ratio, win rate, profit factor, expectancy per trade, and
  per-rule signal accuracy (does Rule 1/2/3 firing actually predict a
  profitable next sell?) — works against either the live `trades.db` or
  `backtest.db`.
- **Dashboard page** in the web UI: equity curve chart, KPI cards, rule
  accuracy table, toggle between Live and Backtest data. Now the default
  landing page.
- **Fixed a real bug**: the buy cooldown check used `datetime.now()` (real
  wall-clock time) instead of the simulated date, which would have silently
  broken cooldown logic in a backtest. Now takes the caller's reference date
  explicitly, correct in both live and backtest use.
- **57 automated tests** (`tests/`, stdlib `unittest`, no network/DB
  dependency): indicator math edge cases, confidence-score weighting,
  exit-rule priority ordering, and KPI calculations on known inputs.

### v1.0 (2026-09-18)

- Portfolio pull + technical screener over a watchlist.
- Full buy + sell dry-run rule engine: buy decisions via a weighted confidence
  score (Rule 1 Trend 40% / Rule 3 MACD 35% / Rule 2 RSI 25%, executes above a
  90% threshold), all 4 exit rules firing independently and immediately.
- Position/day, portfolio/day, and 80%/20% exposure caps; drawdown-based buy
  throttling.
- Full audit trail: ~30 individually-named conditions logged per ticker per
  run, whether they passed or not, correlated by a shared run ID.
- Local web UI (Trades, Decisions, Watchlist, Rules pages), `127.0.0.1` only.
- Daily `launchd` automation for the screener and bot.
- **No live order placement anywhere in the codebase** — this release is
  dry-run only, by design.

Known limitations: earnings-date and bid/ask-spread filters aren't enforced
(no free data source, flagged explicitly every run); trading-day cooldowns
approximate calendar weekdays; daily-loss check compares once-daily snapshots,
not true intraday monitoring. See `TRADING_RULES.md` > Known Implementation
Gaps for details.

## Current status

| Capability | State |
|---|---|
| Pull live portfolio/cash from Trading212 | ✅ working (`t212_portfolio.py`) |
| Technical screener over a watchlist | ✅ working (`screener.py`) |
| Buy rule engine (dry run) | ✅ working (`trading_bot.py`) — **places no real orders** |
| Sell / exit rule engine (dry run) | ✅ working (`trading_bot.py`) — **places no real orders** |
| Live order execution (buy or sell) | ❌ not implemented — no code path calls Trading212's order-placement endpoint at all |
| Trade logging with full indicator snapshot | ✅ working (`trade_db.py`, SQLite) |
| Backtesting | ✅ working (`backtest.py`) — reuses the live bot's own rule functions against historical bars |
| Performance/accuracy KPIs + dashboard | ✅ working (`metrics.py`, web UI Dashboard page) |
| Automated tests | ✅ 57 tests, no network/DB dependency (`tests/`) |

The bot only ever considers tickers listed in `watchlist.csv` — it never scans
the broader market. Add a ticker there (with its Yahoo Finance symbol) to bring
it into scope; remove it to take it out.

## Strategy rules

The full, authoritative rule set — market filter, three buy rules, buy
protection limits, exit rules (specced but not yet coded), execution
requirements, and resolved edge cases — lives in **[`TRADING_RULES.md`](TRADING_RULES.md)**.
Don't duplicate rule changes here; edit that file and this README's summary
below stays a summary.

Quick summary of what's actually implemented today:

- **Market filter**: only buys when SPY > SMA200 AND SPY's SMA50 > SMA200 (bull
  regime). No exceptions.
- **Buy decision = weighted confidence score**, not independent rule firing:
  Rule 1 (Trend, weight 40%), Rule 3 (MACD, weight 35%), Rule 2 (RSI, weight
  25%) each contribute the fraction of their own conditions that are true; a
  buy only executes once the combined score clears 90% (both configurable).
  When it does, it buys a single configurable amount (default €20) — not the
  old per-rule €10/€10/€20. Each rule's individual fired/not-fired status and
  the computed confidence % are still recorded on every trade.
- **Buy protection**: max €20/stock/day, max €50/portfolio/day, max 80%
  portfolio invested / min 20% cash, never adds to a losing position,
  3-trading-day cooldown per stock, blocks all buying at ≥8% drawdown, cuts buy
  size 60% at ≥5% drawdown.
- **Exit rules** (dry-run only, see below): Emergency Stop (-7% → sell 100%, 10
  trading-day re-entry cooldown), Bull Market Profit (+3% → sell 50%, once per
  position instance), Breakout Profit (+10% + new 20d high + volume → sell 25%,
  arms the trailing stop), Trailing Stop (highest price since arming − 2×ATR14).
- **Known gaps**: no free data source for earnings-date or live bid/ask spread,
  so those two Buy Rule 1 conditions aren't enforced yet — flagged explicitly in
  the bot's own output every run, not silently skipped.

Max exposure / min cash is settled: **80% max invested / 20% min cash**,
consistent everywhere in this repo (`algo.csv`, `parameters.csv`,
`TRADING_RULES.md`, and enforced in `trading_bot.py`).

## Project layout

| File | Purpose |
|---|---|
| `t212_portfolio.py` | Pulls live positions + cash from the Trading212 API (Basic auth: key+secret). |
| `market_data.py` | Free, no-key market data + indicators (SMA/EMA/RSI/MACD/ATR) via Yahoo Finance's public chart endpoint. |
| `watchlist.csv` | The whitelist — only these tickers are ever screened or traded. Columns: `t212_ticker, yahoo_symbol, name, notes`. |
| `screener.py` | Scores watchlist tickers 0–100 on a trend + mean-reversion heuristic. Not a prediction — a filter. |
| `trading_bot.py` | Buy + sell dry-run engine implementing `TRADING_RULES.md` (all buy rules, all exit rules, exposure/drawdown/daily-loss gates). No order-placement call exists anywhere in the repo. |
| `config.py` / `rules_config.json` | Editable strategy parameters (position caps, drawdown thresholds, buy/exit rule amounts and percentages). `trading_bot.py` reads this at import time; the web UI writes to it. |
| `webapp.py` + `templates/` | Local web UI (Flask, `127.0.0.1` only) to edit rule values and browse the trade log with full indicator context. See below. |
| `trade_db.py` | SQLite (`trades.db`, gitignored) schema + helpers: `trades` (logged buy/sell with full indicator snapshot), `decisions` (every rule evaluated, fired or not), `equity_snapshots` (peak/drawdown tracking), and `position_state` (exit-rule bookkeeping: bull profit lock, trailing stop, emergency-stop cooldown). |
| `log_trade.py` | CLI to manually log a real trade you placed yourself in the T212 app, auto-filling indicators + P&L. |
| `export_trades.py` | Dumps `trades.db` to CSV for Excel/pandas analysis. |
| `backtest.py` | Runs `trading_bot.py`'s exact rule functions against historical daily bars into an isolated `backtest.db`. See Backtesting below. |
| `metrics.py` | Performance/accuracy KPIs (return, CAGR, drawdown, Sharpe, win rate, profit factor, per-rule signal accuracy) from either `trades.db` or `backtest.db`. |
| `tests/` | 57 automated tests (stdlib `unittest`, no network/DB dependency) covering indicator math, confidence scoring, exit-rule priority, and KPI math. |
| `TRADING_RULES.md` | The authoritative strategy spec. |
| `run_screener.sh` / `run_bot.sh` + `launchd` | Daily automation for the screener and dry-run bot (see below). |
| `gmail_draft.py` / `daily_alert.py` | Optional: create a Gmail draft with the daily screener report. Requires a one-time local OAuth setup (see `gmail_draft.py` docstring) — not yet configured. |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install flask                                          # for webapp.py
.venv/bin/pip install google-auth-oauthlib google-api-python-client  # only needed for gmail_draft.py

cp .env.example .env
# edit .env: T212_API_KEY and T212_SECRET_KEY from Trading212 Settings > API (Beta)
```

Trading212 API auth is HTTP Basic with `base64(api_key:api_secret)` — not a
bearer token. See `t212_portfolio.py`'s `basic_auth_header()`.

## Running things

```bash
.venv/bin/python3 t212_portfolio.py                    # print portfolio summary
.venv/bin/python3 screener.py                           # technical screener report
.venv/bin/python3 trading_bot.py                        # dry-run buy+sell evaluation, logs to trades.db
.venv/bin/python3 log_trade.py --ticker MSFT_US_EQ --action BUY --price 495.17 --qty 0.02 --reason "Buy Rule 1 - Trend"
.venv/bin/python3 export_trades.py trades_export.csv     # dump trade log to CSV
.venv/bin/python3 backtest.py                            # backtest against historical bars, logs to backtest.db
.venv/bin/python3 -m unittest discover -s tests -t .     # run the test suite (57 tests, ~instant)
```

## Backtesting

```bash
.venv/bin/python3 backtest.py --range 5y --capital 1000
```

Reuses `trading_bot.py`'s exact `evaluate_buy()`/`evaluate_exit()` functions
against historical daily bars fetched once per ticker, walked forward day by
day computing indicators from only the data visible up to that day (no
lookahead). Writes to an isolated `backtest.db` (wiped and rebuilt every run),
using the same schema as the live bot, so `metrics.py` and the Dashboard work
identically against either.

**Real limitations** — read before drawing conclusions:
- **Survivorship/lookahead bias**: `watchlist.csv` is today's list, applied
  retroactively across the whole backtest window. You didn't actually have
  these 13 tickers on your radar 5 years ago.
- **No transaction costs, slippage, or realistic fill-price modeling** — fills
  happen at that day's close.
- **Free daily bars only** — no intraday data; relies on Yahoo's own
  adjusted-close handling for splits/dividends.
- `--range` accepts `1y`/`2y`/`5y`/`10y`/`max`; the first 200 trading days of
  whatever you fetch are warmup for SMA200 and aren't simulated.

## Web UI

```bash
.venv/bin/python3 webapp.py
open http://127.0.0.1:5050
```

Local only (binds to `127.0.0.1`, not exposed to your network). Five pages:

- **Dashboard** (default landing page) — equity curve chart, KPI cards (total
  return, CAGR, max drawdown, Sharpe ratio, win rate, profit factor,
  expectancy per trade), and a per-rule signal-accuracy table (does Rule 1/2/3
  firing actually predict a profitable next sell?). Toggle between **Live**
  (your real dry-run history) and **Backtest** (`backtest.db`, run
  `backtest.py` first) data sources.
- **Trades** — filterable table (ticker / BUY-SELL / dry-run vs real) of trades
  that actually got logged, including which of Rule 1/2/3 individually fired and
  the computed confidence % for every BUY row. Each row also has an "All rules"
  expander showing every named condition evaluated that run - buy eligibility
  gates, all Rule 1/2/3 conditions, the confidence check, all 4 exit rules,
  market regime, and portfolio risk gates (~30 individually-named checks) - not
  just the one that produced this trade. Click a timestamp for the raw
  indicator snapshot (SMA/EMA/RSI/MACD/ATR/volume/SPY regime).
- **Decisions** — every rule the bot evaluated, every run, fired or not — not
  just the ones that resulted in a trade. Shows whether a rule *fired* (its own
  conditions were true) and whether it *executed* (a fired rule can still be
  blocked by drawdown, daily loss, exposure cap, or no budget left), with the
  specific block reasons. This is the full audit trail; Trades is just the
  subset that went through.
- **Watchlist** — add or remove tickers without hand-editing `watchlist.csv`.
  Both the Trading212 ticker and Yahoo Finance symbol are required fields, so an
  entry can't end up unscreenable the way a couple of hand-typed rows did before.
- **Rules** — every tunable from `TRADING_RULES.md` (position caps, drawdown
  thresholds, buy amounts, exit thresholds) as an editable form, grouped to match
  the spec's sections. Saves to `rules_config.json`; `trading_bot.py` picks up
  changes on its next run.

## Automation

`run_screener.sh` runs the screener daily via a macOS `launchd` job
(`com.t212.screener`, weekday 7am), writing to `reports/`. `run_bot.sh` runs
`trading_bot.py` the same way (`com.t212.bot`, weekday 7:05am), writing to
`reports/bot_latest.txt`. Both are dry-run/read-only with respect to real
trading, so both are safe to run unattended.

Note: launchd-spawned processes are subject to macOS's TCC privacy protections
for `~/Documents`. If a scheduled job fails with "Operation not permitted",
grant Full Disk Access to `/bin/bash` in System Settings → Privacy & Security.

## Testing

```bash
.venv/bin/python3 -m unittest discover -s tests -t .
```

57 tests, stdlib `unittest`, no network calls and no dependency on `trades.db`
(each test uses its own in-memory SQLite connection or pure function inputs) -
runs in milliseconds. Covers indicator math (SMA/EMA/RSI/MACD/ATR edge cases:
insufficient history, flat prices, all-gains/all-losses), the confidence-score
weighting math, exit-rule priority ordering (emergency stop overrides
everything; trailing stop only evaluated once armed; profit-lock prevents
re-firing), trading-day cooldown counting, and the KPI calculations in
`metrics.py` on known inputs. Run these before trusting a rule change or a
refactor - several were caught by writing this suite (see git history for
`tests/`).

## Safety

- **Nothing in this repo places a real order.** Going live would mean adding a
  new, clearly-separated function that calls Trading212's order-placement
  endpoint — that hasn't been written, and won't be without an explicit,
  deliberate decision to do so.
- **All 4 exit rules are implemented and evaluated every run, but only ever in
  dry run** — a "sell" is a logged decision, not a real order. See
  `TRADING_RULES.md` > "Dry-run sell simulation" for how the bot avoids
  re-firing the same exit forever when the real position never actually
  changes.
- Risk-management rules are meant to override trading signals, not the other
  way around. When a required check can't be verified (e.g. earnings date,
  spread), the bot says so in its output rather than assuming it passes.
- This project does **not guarantee profits or financial returns**. Nothing
  here is financial advice. All strategies should be thoroughly backtested and
  paper-traded before any real capital is used.
