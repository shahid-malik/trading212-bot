# Trading Bot — Defensive Trend/Momentum Strategy

Source of truth for the automated strategy. Any bot/screener logic in this project
should implement exactly this, not a reinterpretation of it. If a rule changes, edit
it here first — including if you change a numeric threshold via the web UI's Rules
page (`webapp.py`), which edits `rules_config.json`. Keep this document and that
file in agreement; this document explains *why* a value is what it is, the config
file is just the value.

Status: **full buy + sell dry-run bot exists** (`trading_bot.py`). It evaluates
every rule below — market filter, all 3 buy rules, all 4 exit rules, the 80/20
exposure cap, drawdown/daily-loss gates — and logs what it *would* do. It places
no real orders of either kind; nothing in this repo calls Trading212's
order-placement endpoint. `backtest.py` runs these exact same rule functions
against historical data (see Backtesting Limitations below), and `metrics.py`
+ the web UI's Dashboard page turn either the live or backtest trade log into
performance KPIs.

### Backtesting Limitations

`backtest.py` reuses `trading_bot.py`'s `evaluate_buy()`/`evaluate_exit()`
directly, so the backtest can't drift from what the live bot actually does -
but the simulation itself has real limitations:

- **Survivorship/lookahead bias**: `watchlist.csv` reflects tickers you're
  watching *today*, applied retroactively across the whole backtest window.
  You didn't actually have this watchlist 5 years ago, and it excludes
  whatever you might have removed or whatever went to zero and dropped off
  your radar.
- **No transaction costs, slippage, spread, or realistic fill modeling** -
  every fill happens exactly at that day's closing price.
- **Free daily bars only** (Yahoo Finance) - no intraday data, and relies on
  Yahoo's own adjusted-close handling for splits/dividends rather than an
  independently verified corporate-actions feed.
- **Earnings-date and spread filters are unenforced here too**, same as live
  (see Known Implementation Gaps below).
- A backtest run is not a promise about future performance. It's a measurement
  of what this specific rule set, against this specific watchlist, would have
  produced over this specific historical window - useful for catching
  obviously broken logic or wildly miscalibrated thresholds, not for
  concluding the strategy "works."

### Dry-run sell simulation — how state survives across runs

Because a dry-run "sell" never touches your real Trading212 position, the real
P&L that triggered an exit rule (e.g. a 7%+ loss) is still there on the next
run. Without extra bookkeeping the bot would re-fire the same exit forever. To
avoid that, `trade_db.position_state` tracks, per ticker:

- `simulated_open` — whether the bot considers this position open *in the
  simulation*. A full-close exit rule (Emergency Stop, Trailing Stop) sets this
  to 0 and records the real average price at that moment.
- On a later run, if `simulated_open` is 0, the bot compares today's real
  average price to the one it recorded at closing time. Unchanged → still the
  same simulated-closed instance, skip re-evaluating. Changed (or the position
  reappeared after being gone) → you actually traded this ticker for real,
  treat it as a fresh instance and resume normal evaluation.
- `bull_profit_lock` and `trailing_stop_active` reset whenever a position is
  marked closed (simulated or real), matching the resolved definition that
  these apply per position instance.

This is bookkeeping for the *simulation's own consistency*, not a claim that it
tracks a parallel paper portfolio precisely — partial-sell amounts in the report
are computed off your real, unaffected position size each run.

### Full condition-level logging

Every individual named condition across every gate and rule is evaluated and
logged every run, whether it passed or not - not just the ones that failed.
Each condition has a unique, fully-qualified name (e.g. `Rule 1 - Price >
EMA20` vs `Rule 3 - Price > EMA20` are logged separately even though they
check the same thing, because each rule's own condition set is independent).
This covers: Buy Eligibility Gates (5), Buy Rule 1/2/3 (5+4+5, including the
2 unenforced Rule 1 conditions), the Confidence check, Market Regime (2),
Portfolio Risk Gates (3), and all 4 Exit Rules (1+2+3+4) - around 30 named
checks per ticker per run. All runs from one execution of `trading_bot.py`
share a `run_id`, so the web UI's Trades page can show the complete breakdown
behind any single trade, not just the rule that fired.

## Capital

- Starting capital: €1,000

## Risk Limits

| Limit | Value |
|---|---|
| Leverage | Never |
| Shorting | Never |
| Averaging down | Never |
| Max invested capital | 80% |
| Min cash held | 20% |
| Max position size per stock | €100 |
| Max initial position (first buy) | €50 |
| Max total portfolio purchases per day | €50 |
| Max purchase per stock per day | €20 |
| Daily portfolio loss limit | 1.5% |
| Drawdown >=5% from peak | Reduce new buy amounts by 60% (superseded from an earlier 50% figure — see [Resolved Definitions](#resolved-definitions)) |
| Drawdown >=8% from peak | Stop all new buying (new and existing positions) |

## Market Filter

Bull market = TRUE only when **both**:
- SPY > SMA200
- SPY SMA50 > SPY SMA200

If Bull market = FALSE:
- Do not open new positions.
- Existing positions may be held; exit/risk rules stay active.

## Buy Rules — Confidence Scoring Model

**Supersedes independent rule firing (2026-09-18).** Buy Rules 1/2/3 no longer
each independently trigger their own buy at their own fixed amount. Instead:

1. Eligibility gates must ALL pass first (see [Buy Protection](#buy-protection)
   below) — bull market, position size, not-losing, buy cooldown, emergency-stop
   cooldown. If any gate fails, nothing buys regardless of score.
2. Each rule's own technical conditions (below) are evaluated as components: the
   fraction of that rule's conditions which are true, times that rule's weight.
3. `confidence % = (rule1_fraction_true × rule1_weight + rule2_fraction_true × rule2_weight + rule3_fraction_true × rule3_weight) / (rule1_weight + rule2_weight + rule3_weight) × 100`
4. A buy executes only when `confidence % >= confidence_threshold_pct` (default
   **90%**), at a single configurable amount (`confidence_buy_amount`, default
   **€20**) — not the old per-rule €10/€10/€20 amounts.

Default weights (editable in the web UI's Rules page / `rules_config.json`):
Rule 1 = 40%, Rule 3 = 35%, Rule 2 = 25% — matching the original precedence
(Rule 1 > Rule 3 > Rule 2).

Every trade still records which of Rule 1/2/3's own condition sets were fully
true (`rule1_fired`/`rule2_fired`/`rule3_fired`) and the computed confidence %,
visible in the web UI's Trades and Decisions pages — so you can see exactly
which signals contributed to a buy, even though they no longer fire alone.

### Buy Rule 1 — Trend (weight: `rule1_weight_pct`, default 40%)

Conditions: `Price > EMA20`, `Price > SMA50`, `SMA50 > SMA200`.

### Buy Rule 2 — RSI (weight: `rule2_weight_pct`, default 25%)

Conditions: `RSI14 >= 55 AND RSI14 <= 65`, `Price > EMA20`, `Price > SMA50`,
`SMA50 > SMA200`.

### Buy Rule 3 — MACD (weight: `rule3_weight_pct`, default 35%)

Conditions: `MACD > Signal`, `MACD Histogram > 0`, `Price > EMA20`,
`Price > SMA50`, `SMA50 > SMA200`.

Not enforced in any rule (no free data source, see Known Implementation Gaps):
`no earnings within 3 trading days`, `spread <= 0.5%`.

## Buy Protection (hard caps, checked before the confidence buy executes)

- Never execute more than €20 total purchases in one stock on the same day
  (`max_stock_buy_per_day`).
- Never execute more than €50 total purchases across the entire portfolio on
  the same day (`max_portfolio_buy_per_day`).
- Never buy a position that is currently losing.
- Never buy if portfolio daily loss >= 1.5%.
- Never buy if portfolio drawdown >= 8%.
- Never buy if earnings are within 3 trading days (not enforced, see above).

## Exit Rules

### Exit Rule 1 — Emergency Stop

```
IF position loss >= 7%
```
→ SELL 100%. Do not immediately re-enter the same stock. Cooldown = 10 trading days.

### Exit Rule 2 — Bull Market Profit

```
IF Bull market = TRUE
AND position profit >= 3%
AND bull profit lock = FALSE
```
→ SELL 50% of current position. Set bull profit lock = TRUE. Do not execute this
rule again for the same position.

### Exit Rule 3 — Breakout Profit

```
IF position profit >= 10%
AND Price > previous 20-day high
AND Volume > 1.5 x 20-day average volume
```
→ SELL 25% of current position. Activate trailing stop.

### Exit Rule 4 — Trailing Stop

```
IF trailing stop is active:
  trailing stop = highest price since activation - 2 x ATR14
IF Price <= trailing stop
```
→ SELL 100% of remaining position.

## Execution Requirements

- Use closed candles only. Never decide from an incomplete/in-progress candle.
- Never place duplicate orders.
- Before every order, check: current position, available cash, existing open
  orders, spread, market hours, risk limits.
- Log every decision with: timestamp, ticker, price, SPY regime, SMA20, EMA20,
  SMA50, SMA200, RSI, MACD, signal, histogram, volume, average volume, ATR,
  position size, profit %, reason for buy/sell, order size, order result.

## Resolved Definitions

Answers to the ambiguities originally listed here, as given by the user on
2026-09-17. These are authoritative and override anything above that conflicts
with them (the risk-limits table has already been updated for #5).

### Buy cooldown

- 3 trading days between discretionary buys of the same stock — one shared
  cooldown for the confidence-based buy decision (not per old-rule).
- Risk-management sells are never restricted by buy cooldown - the 4 exit rules
  evaluate and fire independently of this.

### Daily stock buy cap vs. rule stacking

**Superseded by the Confidence Scoring Model above (2026-09-18).** Rules no
longer fire independently, so there's nothing to stack — there's exactly one
buy decision per ticker per run (the confidence score), capped by
`min(confidence_buy_amount, €20 - stock_buys_today, €50 - portfolio_buys_today)`.
The original precedence (Rule 1 > Rule 3 > Rule 2) lives on as the default
confidence weight ordering (40% > 35% > 25%).

### "Stock is not losing"

- Unrealized P/L >= 0% based on current average entry price.
- `current_price < average_entry_price` → no additional buying.
- Exactly 0% → buying allowed if every other condition passes.
- No existing position → this check is trivially satisfied (nothing to be losing on).

### Bull profit lock reset

- Applies to the current position instance, not the ticker permanently.
- Resets to FALSE once the position is fully closed (100% sold).
- A later re-entry starts with a fresh (FALSE) profit-lock state.
- Partial sells do not reset it.

### Portfolio drawdown calculation

- `portfolio_equity = cash + current_market_value_of_all_positions` (total
  portfolio equity, not invested capital only).
- `peak_equity` = highest historical portfolio equity ever recorded.
- `drawdown = (peak_equity - current_equity) / peak_equity * 100`.
- >=5% drawdown: reduce new buy amounts by 60% (not 50% as originally stated).
- >=8% drawdown: stop all new buying — this reads as blocking buys into both new
  *and* existing positions, since it's listed separately under Buy Protection as an
  unconditional "Never buy if portfolio drawdown >= 8%."

## Known Implementation Gaps

Not ambiguities in the spec — things the spec requires that free data sources
can't currently supply. `trading_bot.py` flags these explicitly in its output
rather than silently assuming they pass:

- **Earnings-date filter** ("no earnings within 3 trading days") — no free,
  reliable earnings-calendar source wired up yet. Currently NOT enforced.
- **Spread <= 0.5% (Buy Rule 1)** — no free live bid/ask feed wired up yet.
  Currently NOT enforced.
- **Daily portfolio loss (1.5%)** — the bot runs once/day, so this compares
  today's snapshot to the previous day's snapshot rather than a true intraday
  circuit breaker. Not equivalent to real-time loss monitoring.
- **Trading-day counts** (cooldowns) approximate trading days as weekdays,
  ignoring market holidays.
