#!/usr/bin/env python3
"""Full DRY-RUN bot implementing TRADING_RULES.md: buy rules 1-3, exit rules 1-4,
and all buy-protection/risk limits, including the 80% max invested / 20% min
cash exposure cap.

Places NO real orders, buy or sell. Every decision below is evaluated against
your live Trading212 cash/positions and current market data, then logged to
trades.db (dry_run=1) and printed as a report. Going live (real order
placement) is a separate, deliberate step this script does not perform -
nothing here calls Trading212's order-placement endpoint.

Position state (bull profit lock, trailing stop, emergency-stop cooldown) is
tracked in trade_db.position_state across runs so the exit rules behave
correctly day over day even though no real shares ever change hands - see the
"Dry-run sell simulation" note in TRADING_RULES.md.

Usage:
  python3 trading_bot.py
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import config as botconfig
import market_data
import t212_portfolio as t212
import trade_db

HERE = Path(__file__).parent

# --- Editable via webapp.py (Rules page) -> rules_config.json -------------------
CFG = botconfig.load_config()

MAX_POSITION_PER_STOCK = CFG["max_position_per_stock"]
MAX_STOCK_BUY_PER_DAY = CFG["max_stock_buy_per_day"]
MAX_PORTFOLIO_BUY_PER_DAY = CFG["max_portfolio_buy_per_day"]
MAX_INVESTED_PCT = CFG["max_invested_pct"]
MIN_CASH_PCT = CFG["min_cash_pct"]
DAILY_LOSS_LIMIT_PCT = CFG["daily_loss_limit_pct"]
DRAWDOWN_REDUCE_PCT = CFG["drawdown_reduce_pct"]
DRAWDOWN_REDUCE_FACTOR = CFG["drawdown_reduce_factor"]  # reduce buy amounts by this fraction
DRAWDOWN_STOP_PCT = CFG["drawdown_stop_pct"]
BUY_COOLDOWN_TRADING_DAYS = int(CFG["buy_cooldown_trading_days"])

# Confidence scoring supersedes independent Rule 1/2/3 firing (see TRADING_RULES.md
# > Buy Rules - Confidence Scoring Model). Each rule's own conditions still get
# evaluated (and shown per-trade), but only the combined weighted score decides
# whether a buy executes.
RULE1_WEIGHT_PCT = CFG["rule1_weight_pct"]
RULE2_WEIGHT_PCT = CFG["rule2_weight_pct"]
RULE3_WEIGHT_PCT = CFG["rule3_weight_pct"]
CONFIDENCE_THRESHOLD_PCT = CFG["confidence_threshold_pct"]
CONFIDENCE_BUY_AMOUNT = CFG["confidence_buy_amount"]
RSI_BUY_MIN = CFG["rsi_buy_min"]
RSI_BUY_MAX = CFG["rsi_buy_max"]

EMERGENCY_STOP_LOSS_PCT = CFG["emergency_stop_loss_pct"]
EMERGENCY_STOP_COOLDOWN_DAYS = int(CFG["emergency_stop_cooldown_days"])
BULL_PROFIT_PCT = CFG["bull_profit_pct"]
BULL_PROFIT_SELL_FRACTION = CFG["bull_profit_sell_fraction"]
BREAKOUT_PROFIT_PCT = CFG["breakout_profit_pct"]
BREAKOUT_SELL_FRACTION = CFG["breakout_sell_fraction"]
BREAKOUT_VOLUME_MULT = CFG["breakout_volume_mult"]
TRAILING_STOP_ATR_MULT = CFG["trailing_stop_atr_mult"]


def trading_days_between(start: datetime, end: datetime) -> int:
    """Approximate trading days as weekdays; ignores market holidays (documented
    gap, see TRADING_RULES.md > Known Implementation Gaps)."""
    days, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def add_trading_days(start: datetime, n: int) -> datetime:
    d, added = start, 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def load_watchlist() -> list[dict]:
    with (HERE / "watchlist.csv").open(newline="") as f:
        return list(csv.DictReader(f))


def get_auth_header() -> str:
    t212.load_dotenv(HERE / ".env")
    api_key = os.environ.get("T212_API_KEY")
    api_secret = os.environ.get("T212_SECRET_KEY")
    if not api_key or not api_secret:
        sys.exit("Error: set T212_API_KEY and T212_SECRET_KEY (.env or env vars).")
    return t212.basic_auth_header(api_key, api_secret)


def _rule_result(rule: str, label: str, conditions: list[tuple[str, bool]], **extra) -> dict:
    """conditions: list of (unique_name, passed). Every condition is kept (not
    just failures) so the full breakdown is always available - see
    TRADING_RULES.md > Buy Rules - Confidence Scoring Model and the web UI's
    Trades page "Show all rules" panel."""
    fired = all(passed for _, passed in conditions)
    fraction = (sum(1 for _, passed in conditions if passed) / len(conditions)) if conditions else 0.0
    blocks = [name for name, passed in conditions if not passed]
    return {"rule": rule, "label": label, "fired": fired, "fraction": fraction,
            "blocks": blocks, "conditions": conditions, **extra}


def market_regime_decision(regime: dict) -> dict:
    conditions = [
        ("Market - SPY > SMA200", regime["spy_price"] > regime["spy_sma200"] if regime["spy_sma200"] else False),
        ("Market - SPY SMA50 > SMA200", (regime["spy_sma50"] > regime["spy_sma200"])
                                         if regime["spy_sma50"] and regime["spy_sma200"] else False),
    ]
    return _rule_result("market", "Market Regime (SPY)", conditions)


def portfolio_gates_decision(drawdown_pct: float, daily_loss_pct: float, exposure_headroom: float) -> dict:
    conditions = [
        ("Portfolio - Drawdown Below Stop (8%)", drawdown_pct < DRAWDOWN_STOP_PCT),
        ("Portfolio - Daily Loss Below Limit (1.5%)", daily_loss_pct < DAILY_LOSS_LIMIT_PCT),
        ("Portfolio - Exposure Headroom Available", exposure_headroom > 0),
    ]
    return _rule_result("portfolio", "Portfolio Risk Gates", conditions)


def evaluate_exit(position: dict, snap: dict, pstate: dict, bull_market: bool) -> list[dict]:
    """Returns exit decisions in priority order, each a _rule_result(). Stops
    early (doesn't evaluate further rules) once a rule closes the whole
    position, since nothing would be left to sell."""
    avg_price = position["averagePrice"]
    current_price = position["currentPrice"]
    profit_pct = (current_price / avg_price - 1) * 100 if avg_price else 0.0
    price = snap["price"]
    decisions = []

    # Exit Rule 1 - Emergency Stop (highest priority, overrides everything else)
    exit1 = _rule_result("exit1", "Exit Rule 1 - Emergency Stop",
                          [("Exit 1 - Loss >= 7%", profit_pct <= -EMERGENCY_STOP_LOSS_PCT)],
                          sell_fraction=1.0, profit_pct=profit_pct, closes_position=True)
    decisions.append(exit1)
    if exit1["fired"]:
        return decisions

    # Exit Rule 4 - Trailing Stop (only relevant if already armed by Rule 3 previously)
    armed = bool(pstate["trailing_stop_active"])
    if armed:
        highest = max(pstate["trailing_stop_highest_price"] or price, price)
        atr14 = snap["atr14"]
        trailing_level = (highest - TRAILING_STOP_ATR_MULT * atr14) if atr14 is not None else None
        triggered = trailing_level is not None and price <= trailing_level
    else:
        highest = trailing_level = None
        triggered = False
    exit4 = _rule_result("exit4", "Exit Rule 4 - Trailing Stop", [
        ("Exit 4 - Trailing Stop Armed", armed),
        ("Exit 4 - Price <= Trailing Level", triggered),
    ], sell_fraction=1.0, profit_pct=profit_pct, closes_position=True, highest=highest, trailing_level=trailing_level)
    exit4["fired"] = armed and triggered  # both conditions must hold, not "all conditions true" (armed=False shouldn't count as N/A pass)
    decisions.append(exit4)
    if exit4["fired"]:
        return decisions

    # Exit Rule 2 - Bull Market Profit (once per position instance)
    exit2 = _rule_result("exit2", "Exit Rule 2 - Bull Market Profit", [
        ("Exit 2 - Bull Market Required", bull_market),
        ("Exit 2 - Profit >= 3%", profit_pct >= BULL_PROFIT_PCT),
        ("Exit 2 - Profit Lock Available", not pstate["bull_profit_lock"]),
    ], sell_fraction=BULL_PROFIT_SELL_FRACTION, profit_pct=profit_pct, closes_position=False)
    decisions.append(exit2)

    # Exit Rule 3 - Breakout Profit (arms the trailing stop; only fires once per instance)
    prev_high = snap.get("prev_20d_high")
    avg_vol = snap.get("avg_volume20")
    exit3 = _rule_result("exit3", "Exit Rule 3 - Breakout Profit", [
        ("Exit 3 - Profit >= 10%", profit_pct >= BREAKOUT_PROFIT_PCT),
        ("Exit 3 - Price > Prior 20D High", prev_high is not None and price > prev_high),
        ("Exit 3 - Volume >= 1.5x Avg", avg_vol is not None and snap["volume"] > BREAKOUT_VOLUME_MULT * avg_vol),
        ("Exit 3 - Trailing Stop Not Already Armed", not pstate["trailing_stop_active"]),
    ], sell_fraction=BREAKOUT_SELL_FRACTION, profit_pct=profit_pct, closes_position=False, arms_trailing_stop=True)
    decisions.append(exit3)

    return decisions


def evaluate_buy(ticker: str, position: dict | None, snap: dict, bull_market: bool,
                  pstate: dict, conn, today: str) -> list[dict]:
    """Returns [gates, rule1, rule2, rule3, confidence]. gates/rule1/rule2/rule3
    are informational only (fired = that rule's own full condition set was
    true) - see TRADING_RULES.md > Buy Rules - Confidence Scoring Model for why
    they no longer trigger independent buys. Only 'confidence' executes."""
    price = snap["price"]

    position_value = (position["quantity"] * position["currentPrice"]) if position else 0.0
    avg_price = position["averagePrice"] if position else None
    not_losing = (avg_price is None) or (position["currentPrice"] >= avg_price)

    last_buy_ts = trade_db.last_buy_timestamp(conn, ticker)
    cooldown_ok = True
    if last_buy_ts:
        # Uses `today` (the caller's reference date), not wall-clock time - the
        # live bot passes real today, backtest.py passes the simulated date, so
        # this works correctly in both without duplicating the logic.
        cooldown_ok = trading_days_between(datetime.fromisoformat(last_buy_ts), datetime.fromisoformat(today)) >= BUY_COOLDOWN_TRADING_DAYS

    emergency_cooldown_ok = True
    if pstate["emergency_stop_until"]:
        emergency_cooldown_ok = today >= pstate["emergency_stop_until"]

    # Eligibility gates: portfolio/position state, not technical signal. All must
    # pass or nothing buys regardless of confidence score (Buy Protection rules).
    gates = _rule_result("gates", "Buy Eligibility Gates", [
        ("Gate - Bull Market Required", bull_market),
        ("Gate - Position Below Max", position_value < MAX_POSITION_PER_STOCK),
        ("Gate - Position Not Losing", not_losing),
        ("Gate - Buy Cooldown Expired", cooldown_ok),
        ("Gate - Emergency-Stop Cooldown Expired", emergency_cooldown_ok),
    ])

    price_above_ema20 = snap["ema20"] is not None and price > snap["ema20"]
    price_above_sma50 = snap["sma50"] is not None and price > snap["sma50"]
    sma50_above_sma200 = snap["sma50"] is not None and snap["sma200"] is not None and snap["sma50"] > snap["sma200"]
    rsi_in_range = snap["rsi14"] is not None and RSI_BUY_MIN <= snap["rsi14"] <= RSI_BUY_MAX
    macd_above_signal = snap["macd"] is not None and snap["macd_signal"] is not None and snap["macd"] > snap["macd_signal"]
    macd_hist_positive = snap["macd_histogram"] is not None and snap["macd_histogram"] > 0

    rule1 = _rule_result("rule1", "Buy Rule 1 - Trend", [
        ("Rule 1 - Price > EMA20", price_above_ema20),
        ("Rule 1 - Price > SMA50", price_above_sma50),
        ("Rule 1 - SMA50 > SMA200", sma50_above_sma200),
        ("Rule 1 - No Earnings Within 3 Days (not enforced)", True),
        ("Rule 1 - Spread <= 0.5% (not enforced)", True),
    ])
    rule2 = _rule_result("rule2", "Buy Rule 2 - RSI", [
        (f"Rule 2 - RSI14 In Range [{RSI_BUY_MIN:g},{RSI_BUY_MAX:g}]", rsi_in_range),
        ("Rule 2 - Price > EMA20", price_above_ema20),
        ("Rule 2 - Price > SMA50", price_above_sma50),
        ("Rule 2 - SMA50 > SMA200", sma50_above_sma200),
    ])
    rule3 = _rule_result("rule3", "Buy Rule 3 - MACD", [
        ("Rule 3 - MACD > Signal", macd_above_signal),
        ("Rule 3 - MACD Histogram > 0", macd_hist_positive),
        ("Rule 3 - Price > EMA20", price_above_ema20),
        ("Rule 3 - Price > SMA50", price_above_sma50),
        ("Rule 3 - SMA50 > SMA200", sma50_above_sma200),
    ])

    # Only the trend/momentum conditions feed the weighted score - eligibility
    # gates are a separate hard pass/fail, not part of the 0-100% gradient.
    total_weight = RULE1_WEIGHT_PCT + RULE2_WEIGHT_PCT + RULE3_WEIGHT_PCT
    weighted = (rule1["fraction"] * RULE1_WEIGHT_PCT) + (rule2["fraction"] * RULE2_WEIGHT_PCT) + (rule3["fraction"] * RULE3_WEIGHT_PCT)
    confidence_pct = (weighted / total_weight * 100) if total_weight else 0.0

    confidence = _rule_result("confidence", "Confidence Buy Rule",
                               [("Confidence - Score >= Threshold", confidence_pct >= CONFIDENCE_THRESHOLD_PCT)],
                               base_amount=CONFIDENCE_BUY_AMOUNT, confidence_pct=confidence_pct,
                               rule1_fired=rule1["fired"], rule2_fired=rule2["fired"], rule3_fired=rule3["fired"])
    confidence["fired"] = confidence["fired"] and gates["fired"]
    confidence["blocks"] = gates["blocks"] + confidence["blocks"]

    return [gates, rule1, rule2, rule3, confidence]


def log_sell(conn, ticker: str, symbol: str, position: dict, snap: dict, d: dict,
             qty: float, amount: float, bull_market: bool, run_id: str) -> int:
    return trade_db.record_trade(
        conn,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
        ticker=ticker, yahoo_symbol=symbol, action="SELL",
        order_size_eur=amount, quantity=qty, price=position["currentPrice"],
        avg_price=position["averagePrice"], current_price=position["currentPrice"],
        profit_loss_pct=d.get("profit_pct"), profit_loss_eur=position["ppl"],
        spy_regime="bull" if bull_market else "bear",
        sma20=snap["sma20"], ema20=snap["ema20"], sma50=snap["sma50"], sma200=snap["sma200"],
        rsi14=snap["rsi14"], macd=snap["macd"], macd_signal=snap["macd_signal"],
        macd_histogram=snap["macd_histogram"], volume=snap["volume"],
        avg_volume20=snap["avg_volume20"], atr14=snap["atr14"], spread_pct=None,
        reason=d["label"], order_result="DRY_RUN", dry_run=1, run_id=run_id,
    )


def log_buy(conn, ticker: str, symbol: str, position: dict | None, snap: dict, d: dict,
            amount: float, bull_market: bool, run_id: str) -> int:
    return trade_db.record_trade(
        conn,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
        ticker=ticker, yahoo_symbol=symbol, action="BUY",
        order_size_eur=amount, quantity=amount / snap["price"], price=snap["price"],
        avg_price=position["averagePrice"] if position else None, current_price=snap["price"],
        profit_loss_pct=(position["currentPrice"] / position["averagePrice"] - 1) * 100 if position else None,
        profit_loss_eur=position["ppl"] if position else None,
        spy_regime="bull" if bull_market else "bear",
        sma20=snap["sma20"], ema20=snap["ema20"], sma50=snap["sma50"], sma200=snap["sma200"],
        rsi14=snap["rsi14"], macd=snap["macd"], macd_signal=snap["macd_signal"],
        macd_histogram=snap["macd_histogram"], volume=snap["volume"],
        avg_volume20=snap["avg_volume20"], atr14=snap["atr14"], spread_pct=None,
        reason=d["label"], order_result="DRY_RUN", dry_run=1, run_id=run_id,
        rule1_fired=int(d["rule1_fired"]), rule2_fired=int(d["rule2_fired"]), rule3_fired=int(d["rule3_fired"]),
        confidence_pct=d["confidence_pct"],
    )


def log_decision(conn, ticker: str, symbol: str, snap: dict, side: str, d: dict,
                  fired: bool, executed: bool, amount: float | None, blocks: list[str],
                  bull_market: bool, run_id: str, trade_id: int | None = None) -> None:
    conditions = d.get("conditions") or []
    trade_db.record_decision(
        conn,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
        ticker=ticker, yahoo_symbol=symbol, side=side, rule=d["rule"], label=d["label"],
        fired=int(fired), executed=int(executed), amount_eur=amount,
        blocks="; ".join(blocks) if blocks else None,
        price=snap["price"], spy_regime="bull" if bull_market else "bear",
        sma20=snap["sma20"], ema20=snap["ema20"], sma50=snap["sma50"], sma200=snap["sma200"],
        rsi14=snap["rsi14"], macd=snap["macd"], macd_signal=snap["macd_signal"],
        macd_histogram=snap["macd_histogram"], volume=snap["volume"],
        avg_volume20=snap["avg_volume20"], atr14=snap["atr14"], trade_id=trade_id, run_id=run_id,
        conditions_json=json.dumps([{"name": n, "passed": bool(p)} for n, p in conditions]),
    )


def main() -> None:
    auth_header = get_auth_header()
    today = time.strftime("%Y-%m-%d")
    run_id = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn = trade_db.get_conn()

    cash = t212.fetch_cash(t212.LIVE_BASE, auth_header)
    positions = t212.fetch_portfolio(t212.LIVE_BASE, auth_header)
    positions_by_ticker = {p["ticker"]: p for p in positions}

    total_equity = cash["total"]
    invested_value = cash["invested"]
    free_cash = cash["free"]
    trade_db.record_equity_snapshot(conn, today, free_cash, invested_value, total_equity)

    peak = trade_db.peak_equity(conn) or total_equity
    drawdown_pct = (peak - total_equity) / peak * 100 if peak else 0.0

    prev_equity = trade_db.previous_equity(conn, today)
    daily_loss_pct = ((prev_equity - total_equity) / prev_equity * 100) if prev_equity else 0.0

    regime = market_data.spy_regime()
    bull_market = regime["bull_market"]

    lines = [f"Trading212 bot (DRY RUN, buy+sell) - {today}",
             "No real orders placed - buy and sell decisions are both simulated and logged only.", ""]
    lines.append(f"Equity: EUR{total_equity:,.2f} (cash EUR{free_cash:,.2f}, invested EUR{invested_value:,.2f})")
    lines.append(f"Peak equity to date: EUR{peak:,.2f} -> drawdown {drawdown_pct:.2f}%")
    if prev_equity:
        lines.append(f"vs. previous snapshot: {daily_loss_pct:+.2f}% (approximation, see Known Implementation Gaps)")
    lines.append(f"SPY regime: {'BULL' if bull_market else 'BEAR'} "
                 f"(SPY={regime['spy_price']:.2f}, SMA50={regime['spy_sma50']:.2f}, SMA200={regime['spy_sma200']:.2f})")

    max_invested_value = total_equity * (MAX_INVESTED_PCT / 100)
    exposure_headroom = max(0.0, max_invested_value - invested_value)
    lines.append(f"Exposure: EUR{invested_value:,.2f} invested / EUR{max_invested_value:,.2f} max "
                 f"({MAX_INVESTED_PCT:.0f}% cap, {MIN_CASH_PCT:.0f}% min cash) -> headroom EUR{exposure_headroom:,.2f}")

    portfolio_blocks = []
    if drawdown_pct >= DRAWDOWN_STOP_PCT:
        portfolio_blocks.append(f"drawdown {drawdown_pct:.2f}% >= {DRAWDOWN_STOP_PCT:.0f}% stop threshold - no buying at all")
    if daily_loss_pct >= DAILY_LOSS_LIMIT_PCT:
        portfolio_blocks.append(f"daily loss {daily_loss_pct:.2f}% >= {DAILY_LOSS_LIMIT_PCT:.1f}% limit - no buying today")
    if exposure_headroom <= 0:
        portfolio_blocks.append(f"portfolio at/above {MAX_INVESTED_PCT:.0f}% max invested - no buying today")

    buy_multiplier = 1.0
    if not portfolio_blocks and drawdown_pct >= DRAWDOWN_REDUCE_PCT:
        buy_multiplier = 1.0 - DRAWDOWN_REDUCE_FACTOR
        lines.append(f"Drawdown >= {DRAWDOWN_REDUCE_PCT:.0f}%: buy amounts reduced to {buy_multiplier*100:.0f}% of normal")

    remaining_portfolio_cap = min(
        MAX_PORTFOLIO_BUY_PER_DAY - trade_db.daily_portfolio_buy_total(conn, today),
        exposure_headroom,
    )
    lines.append(f"Remaining portfolio buy budget today: EUR{remaining_portfolio_cap:.2f} "
                 f"(EUR{MAX_PORTFOLIO_BUY_PER_DAY:.0f}/day cap, EUR{exposure_headroom:.2f} exposure headroom)")
    if portfolio_blocks:
        lines.append("")
        lines.append("ALL BUYING BLOCKED TODAY:")
        for b in portfolio_blocks:
            lines.append(f"  - {b}")

    lines.append("")
    lines.append("NOTE: earnings-date and bid/ask spread filters are NOT enforced (no free data "
                 "source wired up). See TRADING_RULES.md > Known Implementation Gaps.")
    lines.append("")

    # Same for every ticker this run - computed once, logged per ticker so each
    # trade's full rule breakdown (market + portfolio + buy/exit) is self-contained.
    market_dec = market_regime_decision(regime)
    portfolio_dec = portfolio_gates_decision(drawdown_pct, daily_loss_pct, exposure_headroom)

    watchlist = load_watchlist()
    would_buy_total = 0.0
    would_sell_total = 0.0

    for row in watchlist:
        ticker = row["t212_ticker"]
        symbol = (row.get("yahoo_symbol") or "").strip()
        if not symbol:
            lines.append(f"{ticker}: skipped (no yahoo_symbol in watchlist.csv)")
            continue

        try:
            snap = market_data.indicator_snapshot(symbol)
        except market_data.MarketDataError as e:
            lines.append(f"{ticker}: skipped ({e})")
            continue
        time.sleep(0.3)

        position = positions_by_ticker.get(ticker)
        pstate = trade_db.get_position_state(conn, ticker)

        lines.append(f"{ticker} ({symbol})  price={snap['price']:.2f}  "
                     f"position=EUR{(position['quantity']*position['currentPrice']) if position else 0:.2f}")

        log_decision(conn, ticker, symbol, snap, "BUY", market_dec, fired=market_dec["fired"], executed=False,
                     amount=None, blocks=market_dec["blocks"], bull_market=bull_market, run_id=run_id)
        log_decision(conn, ticker, symbol, snap, "BUY", portfolio_dec, fired=portfolio_dec["fired"], executed=False,
                     amount=None, blocks=portfolio_dec["blocks"], bull_market=bull_market, run_id=run_id)

        # --- Exit rules first: risk management overrides trading signals ---
        position_closed_today = False
        if position:
            # The dry-run "sell" never touches the real position, so on the next run
            # the same loss/profit condition would fire again forever. Detect that
            # by comparing today's real average price to the one recorded when we
            # last simulated a close: unchanged -> still the same closed instance,
            # skip re-evaluating; changed (or the position had vanished and came
            # back) -> you actually traded this for real, treat as a fresh instance.
            if not pstate["simulated_open"]:
                real_change = (pstate["last_known_avg_price"] is None or
                               abs(position["averagePrice"] - pstate["last_known_avg_price"]) > 1e-6)
                if real_change:
                    trade_db.reopen_position_state(conn, ticker, position["averagePrice"])
                    pstate = trade_db.get_position_state(conn, ticker)
                    lines.append("    (real position change detected since simulated close - treating as a new instance)")
                else:
                    lines.append(f"    (already closed in simulation: {pstate['close_reason']} - "
                                 "skipping exit re-evaluation until the real position changes)")
                    position_closed_today = True

        if position and not position_closed_today:
            exit_decisions = evaluate_exit(position, snap, pstate, bull_market)
            remaining_fraction = 1.0
            for d in exit_decisions:
                if not d["fired"]:
                    lines.append(f"    {d['label']}: no (" + "; ".join(d["blocks"]) + ")")
                    log_decision(conn, ticker, symbol, snap, "SELL", d, fired=False, executed=False,
                                 amount=None, blocks=d["blocks"], bull_market=bull_market, run_id=run_id)
                    continue

                sell_frac_of_original = remaining_fraction * d["sell_fraction"]
                qty = position["quantity"] * sell_frac_of_original
                amount = qty * position["currentPrice"]
                lines.append(f"    {d['label']}: SELL {d['sell_fraction']*100:.0f}% of remaining "
                             f"(EUR{amount:.2f}, dry run)")
                trade_id = log_sell(conn, ticker, symbol, position, snap, d, qty, amount, bull_market, run_id)
                log_decision(conn, ticker, symbol, snap, "SELL", d, fired=True, executed=True,
                             amount=amount, blocks=[], bull_market=bull_market, run_id=run_id, trade_id=trade_id)
                would_sell_total += amount
                remaining_fraction *= (1 - d["sell_fraction"])

                if d.get("closes_position"):
                    if d["rule"] == "exit1":
                        emergency_until = add_trading_days(datetime.fromisoformat(today), EMERGENCY_STOP_COOLDOWN_DAYS).strftime("%Y-%m-%d")
                        trade_db.mark_simulated_closed(conn, ticker, d["label"], position["averagePrice"])
                        trade_db.set_position_state(conn, ticker, emergency_stop_until=emergency_until)
                        lines.append(f"        -> position closed (simulated), re-entry blocked until {emergency_until}")
                    else:
                        trade_db.mark_simulated_closed(conn, ticker, d["label"], position["averagePrice"])
                        lines.append("        -> position closed (simulated), profit-lock/trailing-stop state reset")
                    position_closed_today = True
                    break
                elif d["rule"] == "exit2":
                    trade_db.set_position_state(conn, ticker, bull_profit_lock=1)
                elif d["rule"] == "exit3":
                    trade_db.set_position_state(conn, ticker, trailing_stop_active=1,
                                                 trailing_stop_highest_price=position["currentPrice"])

            pstate = trade_db.get_position_state(conn, ticker)  # refresh after any updates

        # --- Buy rules: always evaluated (so every rule that "took part" is
        # recorded), execution is gated separately so a blocked rule still
        # shows up as fired=1/executed=0 with the reason it didn't go through ---
        if position_closed_today:
            lines.append("    (buy rules evaluated below, but position was closed by an exit rule above today)")
        if portfolio_blocks:
            lines.append("    (buy rules evaluated below, but blocked by the portfolio-level gate above)")

        remaining_stock_cap = MAX_STOCK_BUY_PER_DAY - trade_db.daily_stock_buy_total(conn, ticker, today)
        buy_decisions = {d["rule"]: d for d in evaluate_buy(ticker, position, snap, bull_market, pstate, conn, today)}

        gates = buy_decisions["gates"]
        lines.append(f"    {gates['label']}: {'yes' if gates['fired'] else 'no'}"
                     + (f" ({'; '.join(gates['blocks'])})" if gates["blocks"] else ""))
        log_decision(conn, ticker, symbol, snap, "BUY", gates, fired=gates["fired"], executed=False,
                     amount=None, blocks=gates["blocks"], bull_market=bull_market, run_id=run_id)

        # Rule 1/2/3 no longer trigger independent buys - each is just a
        # component of the weighted confidence score below. Still logged for
        # visibility (see TRADING_RULES.md > Buy Rules - Confidence Scoring Model).
        for rule_key in ("rule1", "rule2", "rule3"):
            comp = buy_decisions[rule_key]
            lines.append(f"    {comp['label']}: {'yes' if comp['fired'] else 'no'}"
                         + (f" ({'; '.join(comp['blocks'])})" if comp["blocks"] else ""))
            log_decision(conn, ticker, symbol, snap, "BUY", comp, fired=comp["fired"], executed=False,
                         amount=None, blocks=comp["blocks"], bull_market=bull_market, run_id=run_id)

        d = buy_decisions["confidence"]
        confidence_note = f"confidence {d['confidence_pct']:.1f}% (threshold {CONFIDENCE_THRESHOLD_PCT:.0f}%)"
        lines.append(f"    {d['label']}: {confidence_note}")

        if not d["fired"]:
            lines.append(f"        no buy (" + "; ".join(d["blocks"]) + ")")
            log_decision(conn, ticker, symbol, snap, "BUY", d, fired=False, executed=False,
                         amount=None, blocks=[confidence_note] + d["blocks"], bull_market=bull_market, run_id=run_id)
        else:
            gate_blocks = []
            if position_closed_today:
                gate_blocks.append("position closed by an exit rule earlier this run")
            if portfolio_blocks:
                gate_blocks.extend(portfolio_blocks)
            if gate_blocks:
                lines.append(f"        WOULD fire, but blocked - " + "; ".join(gate_blocks))
                log_decision(conn, ticker, symbol, snap, "BUY", d, fired=True, executed=False,
                             amount=None, blocks=[confidence_note] + gate_blocks, bull_market=bull_market, run_id=run_id)
            else:
                amount = min(d["base_amount"] * buy_multiplier, remaining_stock_cap, remaining_portfolio_cap)
                if amount <= 0:
                    budget_blocks = [f"no budget left (stock cap left EUR{remaining_stock_cap:.2f}, "
                                      f"portfolio cap left EUR{remaining_portfolio_cap:.2f})"]
                    lines.append(f"        WOULD fire, but no budget left "
                                 f"(stock cap left EUR{remaining_stock_cap:.2f}, portfolio cap left EUR{remaining_portfolio_cap:.2f})")
                    log_decision(conn, ticker, symbol, snap, "BUY", d, fired=True, executed=False,
                                 amount=None, blocks=[confidence_note] + budget_blocks, bull_market=bull_market, run_id=run_id)
                else:
                    lines.append(f"        BUY EUR{amount:.2f} (dry run)")
                    trade_id = log_buy(conn, ticker, symbol, position, snap, d, amount, bull_market, run_id)
                    log_decision(conn, ticker, symbol, snap, "BUY", d, fired=True, executed=True,
                                 amount=amount, blocks=[confidence_note], bull_market=bull_market, run_id=run_id, trade_id=trade_id)
                    remaining_stock_cap -= amount
                    remaining_portfolio_cap -= amount
                    would_buy_total += amount

    lines.append("")
    lines.append(f"Total dry-run buys today: EUR{would_buy_total:.2f}")
    lines.append(f"Total dry-run sells today: EUR{would_sell_total:.2f}")

    conn.close()
    print("\n".join(lines))


if __name__ == "__main__":
    main()
