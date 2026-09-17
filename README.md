# Defensive Trading Bot for Trading 212

An algorithmic trading toolkit for Trading 212 focused on **defensive, rule-based
trend and momentum trading**. It combines market-regime analysis, technical
indicators, position sizing, portfolio risk management, and a full buy+sell
dry-run bot — plus the plumbing to pull your live portfolio, screen a
watchlist, and log every (simulated) trade with full indicator context for later
analysis.

## Current status

| Capability | State |
|---|---|
| Pull live portfolio/cash from Trading212 | ✅ working (`t212_portfolio.py`) |
| Technical screener over a watchlist | ✅ working (`screener.py`) |
| Buy rule engine (dry run) | ✅ working (`trading_bot.py`) — **places no real orders** |
| Sell / exit rule engine (dry run) | ✅ working (`trading_bot.py`) — **places no real orders** |
| Live order execution (buy or sell) | ❌ not implemented — no code path calls Trading212's order-placement endpoint at all |
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
```

## Web UI

```bash
.venv/bin/python3 webapp.py
open http://127.0.0.1:5050
```

Local only (binds to `127.0.0.1`, not exposed to your network). Four pages:

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
