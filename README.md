# Defensive Trading Bot for Trading 212

An algorithmic trading toolkit for Trading 212 focused on **defensive, rule-based
trend and momentum trading**. It combines market-regime analysis, technical
indicators, position sizing, portfolio risk management, and (currently) a
buy-only dry-run bot — plus the plumbing to pull your live portfolio, screen a
watchlist, and log every (simulated) trade with full indicator context for later
analysis.

## Current status

| Capability | State |
|---|---|
| Pull live portfolio/cash from Trading212 | ✅ working (`t212_portfolio.py`) |
| Technical screener over a watchlist | ✅ working (`screener.py`) |
| Buy-only rule engine (dry run) | ✅ working (`trading_bot.py`) — **places no real orders** |
| Sell / exit rules | ❌ not implemented — intentionally, per explicit instruction not to automate any selling |
| Live order execution | ❌ not implemented — no code path calls Trading212's order-placement endpoint at all |
| Trade logging with full indicator snapshot | ✅ working (`trade_db.py`, SQLite) |
| Backtesting | ❌ not built yet |

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
- **Buy Rule 1 — Trend** (€10): price > EMA20/SMA50, SMA50 > SMA200.
- **Buy Rule 2 — RSI** (€10): RSI14 between 55–65, same trend conditions.
- **Buy Rule 3 — MACD** (€20): MACD > signal and histogram > 0, same trend
  conditions.
- **Buy protection**: max €20/stock/day, max €50/portfolio/day, never adds to a
  losing position, 3-trading-day cooldown per stock, blocks all buying at ≥8%
  drawdown, cuts buy size 60% at ≥5% drawdown.
- **Rule precedence** when multiple rules fire the same stock/day: Rule 1 > Rule
  3 > Rule 2, capped at €20/stock/day total (they don't stack).
- **Known gaps**: no free data source for earnings-date or live bid/ask spread,
  so those two Buy Rule 1 conditions aren't enforced yet — flagged explicitly in
  the bot's own output every run, not silently skipped.

### ⚠️ Open discrepancy — not yet resolved

This repo's original scaffolding (`algo.csv`, `parameters.csv`, and this
README's earlier draft) specifies **70% max exposure / 30% min cash**. The rules
given explicitly for `TRADING_RULES.md` specify **80% max exposure / 20% min
cash**. `trading_bot.py` currently does not enforce either figure directly (it
gates on per-stock/per-day euro caps and portfolio drawdown, not an aggregate
exposure %) — but this needs a decision before that check gets added. Pick one
source of truth and this note goes away.

## Project layout

| File | Purpose |
|---|---|
| `t212_portfolio.py` | Pulls live positions + cash from the Trading212 API (Basic auth: key+secret). |
| `market_data.py` | Free, no-key market data + indicators (SMA/EMA/RSI/MACD/ATR) via Yahoo Finance's public chart endpoint. |
| `watchlist.csv` | The whitelist — only these tickers are ever screened or traded. Columns: `t212_ticker, yahoo_symbol, name, notes`. |
| `screener.py` | Scores watchlist tickers 0–100 on a trend + mean-reversion heuristic. Not a prediction — a filter. |
| `trading_bot.py` | Buy-only dry-run engine implementing `TRADING_RULES.md`. No sell logic exists in the file; no order-placement call exists anywhere in the repo. |
| `trade_db.py` | SQLite (`trades.db`, gitignored) schema + helpers: trade log with full indicator snapshot, plus an `equity_snapshots` table for peak/drawdown tracking. |
| `log_trade.py` | CLI to manually log a real trade you placed yourself in the T212 app, auto-filling indicators + P&L. |
| `export_trades.py` | Dumps `trades.db` to CSV for Excel/pandas analysis. |
| `TRADING_RULES.md` | The authoritative strategy spec. |
| `run_screener.sh` + `launchd` | Daily automation for the screener (see below). |
| `gmail_draft.py` / `daily_alert.py` | Optional: create a Gmail draft with the daily screener report. Requires a one-time local OAuth setup (see `gmail_draft.py` docstring) — not yet configured. |

## Setup

```bash
python3 -m venv .venv
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
.venv/bin/python3 trading_bot.py                        # dry-run buy-rule evaluation, logs to trades.db
.venv/bin/python3 log_trade.py --ticker MSFT_US_EQ --action BUY --price 495.17 --qty 0.02 --reason "Buy Rule 1 - Trend"
.venv/bin/python3 export_trades.py trades_export.csv     # dump trade log to CSV
```

## Automation

`run_screener.sh` runs the screener daily via a macOS `launchd` job (weekday
7am), writing to `reports/`. `trading_bot.py` is not yet wired into that
schedule — it's currently a manual/on-demand dry run.

Note: launchd-spawned processes are subject to macOS's TCC privacy protections
for `~/Documents`. If a scheduled job fails with "Operation not permitted",
grant Full Disk Access to `/bin/bash` in System Settings → Privacy & Security.

## Safety

- **Nothing in this repo places a real order.** Going live would mean adding a
  new, clearly-separated function that calls Trading212's order-placement
  endpoint — that hasn't been written, and won't be without an explicit,
  deliberate decision to do so.
- **No sell logic exists at all**, automated or otherwise, per explicit
  instruction. Exit rules are specced in `TRADING_RULES.md` but not implemented.
- Risk-management rules are meant to override trading signals, not the other
  way around. When a required check can't be verified (e.g. earnings date,
  spread), the bot says so in its output rather than assuming it passes.
- This project does **not guarantee profits or financial returns**. Nothing
  here is financial advice. All strategies should be thoroughly backtested and
  paper-traded before any real capital is used.
