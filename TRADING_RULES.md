# Trading Bot — Defensive Trend/Momentum Strategy

Source of truth for the automated strategy. Any bot/screener logic in this project
should implement exactly this, not a reinterpretation of it. If a rule changes, edit
it here first.

Status: **buy-only, dry-run bot exists** (`trading_bot.py`). It evaluates these
rules and logs what it *would* buy, but places no real orders and never sells.
Exit rules below are not implemented in code yet — pending explicit instruction
to build them.

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

## Buy Rules

A position may only be opened/added to when Bull market = TRUE and all
[Buy Protection](#buy-protection) checks pass, in addition to each rule's own
conditions.

### Buy Rule 1 — Trend

```
Price > EMA20
AND Price > SMA50
AND SMA50 > SMA200
AND Bull market = TRUE
AND position < €100
AND stock is not losing
AND no earnings within 3 trading days
AND spread <= 0.5%
```
→ BUY €10. Cooldown = 3 trading days (per stock, this rule).

### Buy Rule 2 — RSI

```
RSI14 >= 55 AND RSI14 <= 65
AND Price > EMA20
AND Price > SMA50
AND SMA50 > SMA200
AND Bull market = TRUE
AND position < €100
AND stock is not losing
AND RSI buy cooldown expired
```
→ BUY €10.

### Buy Rule 3 — MACD

```
MACD > Signal
AND MACD Histogram > 0
AND Price > EMA20
AND Price > SMA50
AND SMA50 > SMA200
AND Bull market = TRUE
AND position < €100
AND stock is not losing
AND MACD cooldown expired
```
→ BUY €20.

## Buy Protection (hard caps, checked before any buy rule executes)

- Never execute more than €20 total purchases in one stock on the same day.
- Never execute more than €50 total purchases across the entire portfolio on the same day.
- Never buy a position that is currently losing.
- Never buy if portfolio daily loss >= 1.5%.
- Never buy if portfolio drawdown >= 8%.
- Never buy if earnings are within 3 trading days.

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

- 3 trading days between discretionary buys of the same stock, shared across all
  three buy rules (not a separate cooldown per rule).
- Risk-management sells are never restricted by buy cooldown (moot right now since
  no sell logic is implemented).

### Daily stock buy cap vs. rule stacking

- The €20/stock/day cap always wins — signals never stack past it.
- Precedence when multiple rules fire the same day on the same stock:
  **Rule 1 (strongest confirmed setup) > Rule 3 (MACD/RSI confirmation) > Rule 2
  (basic trend entry)**.
- `daily_stock_buy_amount = min(signal_amount, €20 - stock_buys_today)`. If Rule 1
  consumes the full €20, Rules 2/3 place €0 that day.

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
