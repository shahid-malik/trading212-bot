# Session Handoff — 2026-09-18

Read this first when picking the project back up. It's a "where we left off"
pointer, not a duplicate of `README.md` / `TRADING_RULES.md` — those two are
still the authoritative docs for what the system does and why.

## Right now

- **Branch:** `develop`, in sync with `origin/develop` (nothing unpushed as
  commits — last pushed commit is `678f75e`).
- **Uncommitted local changes** (per explicit instruction this session: *"don't
  push anything on git"* — these were deliberately left staged-but-not-committed):
  - `templates/base.html`, `dashboard.html`, `decisions.html`, `trades.html`,
    `watchlist.html` — a full visual redesign (v2, Gmail/Material style:
    light header, 4 semantic status colors, pill buttons/badges, Roboto +
    Roboto Mono) applied from the new `design-system/` folder.
  - `design-system/` (untracked) — the source tokens/components/README this
    redesign came from. Keep it in the repo; it's the reference for any
    future template changes, not a one-time scratch folder.
  - **Next step**: review the redesign in a browser, then
    `git add -A && git commit -m "..." && git push origin develop` when ready.
    Nothing else needs to change — the README in `design-system/` documents
    exactly what was and wasn't applied (Trades table Columns picker and an
    active dark-mode toggle are the two deliberately-skipped pieces).
- **`main` is 11 commits behind `develop`** — still on the very first
  buy-only bot, no backtesting/dashboard/tests/confidence-scoring/diversified
  watchlist. Merge when you're ready to promote; hasn't come up as a blocker,
  just flagging it's been sitting there for a while.
- **Web UI was running** at `http://127.0.0.1:5050` (pid may be stale by the
  time you resume — just restart it, see below).
- **`launchd` jobs registered and enabled**: `com.t212.screener` (weekday
  7am) and `com.t212.bot` (weekday 7:05am) — both still dry-run only, will
  keep firing on schedule whether or not a session is open.

## Resume commands

```bash
cd /Users/shahid/Documents/businesses/trading/t212

# Web UI
.venv/bin/python3 webapp.py &
open http://127.0.0.1:5050

# Tests (should be 75, ~instant, no network)
.venv/bin/python3 -m unittest discover -s tests -t .

# Manual dry-run bot / screener
.venv/bin/python3 trading_bot.py
.venv/bin/python3 screener.py

# Backtest (fetches live data, takes ~30-60s for 19 tickers x 5y)
.venv/bin/python3 backtest.py --range 5y --capital 1000
```

## Current live strategy config

`rules_config.json` has been tuned extensively via the Rules UI page this
session (upside-oriented, away from the original conservative defaults). As
of now:

| Parameter | Value | Original default |
|---|---|---|
| `max_position_per_stock` | €100 | €100 |
| `max_stock_buy_per_day` | €25 | €20 |
| `max_portfolio_buy_per_day` | €50 | €50 |
| `max_invested_pct` | 90% | 80% |
| `min_cash_pct` | 10% | 20% |
| `buy_cooldown_trading_days` | 1 | 3 |
| `confidence_threshold_pct` | 80% | 90% |
| `confidence_buy_amount` | €30 | €20 |
| `bull_profit_pct` | 15% | 3% |
| `bull_profit_sell_fraction` | 0.25 | 0.5 |
| `breakout_profit_pct` | 20% | 10% |
| `breakout_sell_fraction` | 0.15 | 0.25 |
| `trailing_stop_atr_mult` | 3.5 | 2.0 |
| `emergency_stop_loss_pct` | 10% | 7% |
| `emergency_stop_cooldown_days` | 3 | 10 |

`rule4_weight_pct`/`rule5_weight_pct`/`rule6_weight_pct`/`volume_confirm_mult`
aren't in the saved `rules_config.json` yet (added after the last UI save) —
they're silently using `config.py`'s defaults (15%/15%/15%/1.3). Not a bug,
just means the Rules page will show defaults for those four until someone
hits Save there again.

## What got built this session (chronological, high level)

1. Trading212 portfolio pull, technical screener, buy-only dry-run bot (v1.0).
2. Full buy+sell dry-run engine, exit rules, position-state tracking for
   dry-run-only simulation consistency.
3. Local web UI (Flask): Trades, Decisions, Watchlist, Rules pages.
4. Confidence-scoring model replacing independent rule firing — grew from 3
   rules to **6** (Trend, RSI, MACD, Volume Confirmation, Short-Term
   Momentum, Relative Strength vs SPY).
5. Backtesting engine (`backtest.py`) reusing the live bot's exact rule
   functions against historical bars — found and fixed a real bug along the
   way (trade timestamps used wall-clock time instead of the simulated date,
   which broke cooldown logic and silently capped the backtest at 13 total
   trades ever).
6. Performance/accuracy KPIs + Dashboard page (`metrics.py`): return, CAGR,
   drawdown, Sharpe, win rate, profit factor, per-rule signal accuracy.
7. 75-test suite (`tests/`, stdlib `unittest`, no network dependency).
8. Watchlist diversified from 13 (~all mega-cap tech) to 19 tickers —
   healthcare, banking, financials, energy, consumer staples added,
   explicitly excluding weapons/defense, adult entertainment, and
   alcohol/pork producers per direct instruction.
9. Parameter tuning experiments (upside vs. defensive tradeoff), all
   documented honestly in `TRADING_RULES.md` including where it *didn't*
   close the gap to buy-and-hold and why.
10. This session's last piece: the v2 visual redesign (uncommitted, see above).

## Known open items / good next-session candidates

- Commit + push the design system redesign (see above).
- Decide whether/when to merge `develop` → `main`.
- Trades table **Columns picker** — designed in `design-system/components.css`
  (`.table-toolbar`, `.col-picker-*`, `table.wide`) but the JS/markup isn't
  wired into `templates/trades.html` yet. Needed once more rule columns get
  added (currently 6 hardcoded columns; the pattern scales toward the ~34
  named conditions the bot actually tracks per trade).
- Dark mode: tokens fully defined (`[data-theme="dark"]` in `tokens.css`),
  no UI toggle wired up yet.
- `TRADING_RULES.md` → "Known Implementation Gaps": earnings-date and
  bid/ask-spread filters still unenforced (no free data source found yet).
- Still fully dry-run — no code path anywhere places a real order. Going
  live would be a new, deliberate, separate piece of work, not a flag flip.
- A 6-rule confidence model tuned/backtested on one specific 5-year window is
  real curve-fitting risk — worth testing against a different time window
  before trusting current parameters over the original defaults.
