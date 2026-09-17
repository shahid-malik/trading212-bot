#!/usr/bin/env python3
"""Automated buy-only DRY-RUN bot implementing TRADING_RULES.md.

Places NO real orders. It evaluates Buy Rules 1-3 and all Buy Protection limits
against your live Trading212 cash/positions and the current market data, then
logs what it WOULD have bought to trades.db (dry_run=1) and prints a report.

There is no sell logic in this file at all - not disabled, not stubbed, simply
not written - per instruction to never sell anything automatically. Exit rules in
TRADING_RULES.md remain unimplemented until you explicitly ask for that.

Going live (real order placement) is a separate, deliberate step this script does
not perform. Nothing here calls Trading212's order-placement endpoint.

Usage:
  python3 trading_bot.py
"""

from __future__ import annotations

import csv
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import market_data
import t212_portfolio as t212
import trade_db

HERE = Path(__file__).parent

# --- TRADING_RULES.md constants -------------------------------------------------
MAX_POSITION_PER_STOCK = 100.0
MAX_STOCK_BUY_PER_DAY = 20.0
MAX_PORTFOLIO_BUY_PER_DAY = 50.0
DAILY_LOSS_LIMIT_PCT = 1.5
DRAWDOWN_REDUCE_PCT = 5.0
DRAWDOWN_REDUCE_FACTOR = 0.60  # reduce buy amounts by 60% -> multiplier 0.40
DRAWDOWN_STOP_PCT = 8.0
BUY_COOLDOWN_TRADING_DAYS = 3

RULE1_AMOUNT = 10.0
RULE2_AMOUNT = 10.0
RULE3_AMOUNT = 20.0
# Precedence per TRADING_RULES.md Resolved Definitions: Rule 1 > Rule 3 > Rule 2
RULE_PRECEDENCE = ["rule1", "rule3", "rule2"]


def trading_days_between(start: datetime, end: datetime) -> int:
    """Approximate trading days as weekdays; ignores market holidays (documented
    gap, see TRADING_RULES.md > Known Implementation Gaps)."""
    days, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


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


def evaluate_ticker(row: dict, position: dict | None, snap: dict, bull_market: bool,
                     conn, today: str) -> list[dict]:
    """Returns a list of decisions (fired or blocked) for this ticker, most to
    least significant. Each dict has: rule, fired(bool), amount, block_reason."""
    ticker = row["t212_ticker"]
    price = snap["price"]
    decisions = []

    position_value = (position["quantity"] * position["currentPrice"]) if position else 0.0
    avg_price = position["averagePrice"] if position else None
    not_losing = (avg_price is None) or (position["currentPrice"] >= avg_price)

    last_buy_ts = trade_db.last_buy_timestamp(conn, ticker)
    cooldown_ok = True
    if last_buy_ts:
        last_buy_date = datetime.fromisoformat(last_buy_ts)
        cooldown_ok = trading_days_between(last_buy_date, datetime.now()) >= BUY_COOLDOWN_TRADING_DAYS

    shared_blocks = []
    if not bull_market:
        shared_blocks.append("bull market filter = FALSE")
    if position_value >= MAX_POSITION_PER_STOCK:
        shared_blocks.append(f"position already at/above max (EUR{position_value:.2f} >= EUR{MAX_POSITION_PER_STOCK:.0f})")
    if not not_losing:
        pl_pct = (position["currentPrice"] / avg_price - 1) * 100
        shared_blocks.append(f"position is losing ({pl_pct:+.1f}%)")
    if not cooldown_ok:
        shared_blocks.append(f"buy cooldown active (last buy {last_buy_ts})")
    trend_ok = snap["sma50"] is not None and snap["sma200"] is not None and snap["sma50"] > snap["sma200"]
    if not trend_ok:
        shared_blocks.append("SMA50 not above SMA200")
    price_ok = snap["ema20"] is not None and price > snap["ema20"] and snap["sma50"] is not None and price > snap["sma50"]
    if not price_ok:
        shared_blocks.append("price not above EMA20/SMA50")

    # Rule 1 - Trend (spread/earnings filters not enforced - no data source, see TRADING_RULES.md)
    rule1_fired = not shared_blocks
    decisions.append({"rule": "rule1", "label": "Buy Rule 1 - Trend", "fired": rule1_fired,
                       "base_amount": RULE1_AMOUNT, "blocks": list(shared_blocks) if not rule1_fired else []})

    # Rule 2 - RSI
    rsi_blocks = list(shared_blocks)
    if snap["rsi14"] is None or not (55 <= snap["rsi14"] <= 65):
        rsi_blocks.append(f"RSI14 not in [55,65] (RSI14={snap['rsi14']})")
    rule2_fired = not rsi_blocks
    decisions.append({"rule": "rule2", "label": "Buy Rule 2 - RSI", "fired": rule2_fired,
                       "base_amount": RULE2_AMOUNT, "blocks": rsi_blocks if not rule2_fired else []})

    # Rule 3 - MACD
    macd_blocks = list(shared_blocks)
    if snap["macd"] is None or snap["macd_signal"] is None or not (snap["macd"] > snap["macd_signal"]):
        macd_blocks.append("MACD not above signal")
    if snap["macd_histogram"] is None or not (snap["macd_histogram"] > 0):
        macd_blocks.append("MACD histogram not positive")
    rule3_fired = not macd_blocks
    decisions.append({"rule": "rule3", "label": "Buy Rule 3 - MACD", "fired": rule3_fired,
                       "base_amount": RULE3_AMOUNT, "blocks": macd_blocks if not rule3_fired else []})

    return decisions


def main() -> None:
    auth_header = get_auth_header()
    today = time.strftime("%Y-%m-%d")
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

    lines = [f"Trading212 bot (DRY RUN, buy-only) - {today}",
             "No real orders placed. No sell logic exists in this bot.", ""]
    lines.append(f"Equity: EUR{total_equity:,.2f} (cash EUR{free_cash:,.2f}, invested EUR{invested_value:,.2f})")
    lines.append(f"Peak equity to date: EUR{peak:,.2f} -> drawdown {drawdown_pct:.2f}%")
    if prev_equity:
        lines.append(f"vs. previous snapshot: {daily_loss_pct:+.2f}% (approximation, see Known Implementation Gaps)")
    lines.append(f"SPY regime: {'BULL' if bull_market else 'BEAR'} "
                 f"(SPY={regime['spy_price']:.2f}, SMA50={regime['spy_sma50']:.2f}, SMA200={regime['spy_sma200']:.2f})")

    # Portfolio-level buy protection gates
    portfolio_blocks = []
    if drawdown_pct >= DRAWDOWN_STOP_PCT:
        portfolio_blocks.append(f"drawdown {drawdown_pct:.2f}% >= {DRAWDOWN_STOP_PCT:.0f}% stop threshold - no buying at all")
    if daily_loss_pct >= DAILY_LOSS_LIMIT_PCT:
        portfolio_blocks.append(f"daily loss {daily_loss_pct:.2f}% >= {DAILY_LOSS_LIMIT_PCT:.1f}% limit - no buying today")
    buy_multiplier = 1.0
    if not portfolio_blocks and drawdown_pct >= DRAWDOWN_REDUCE_PCT:
        buy_multiplier = 1.0 - DRAWDOWN_REDUCE_FACTOR
        lines.append(f"Drawdown >= {DRAWDOWN_REDUCE_PCT:.0f}%: buy amounts reduced to {buy_multiplier*100:.0f}% of normal")

    remaining_portfolio_cap = MAX_PORTFOLIO_BUY_PER_DAY - trade_db.daily_portfolio_buy_total(conn, today)
    lines.append(f"Remaining portfolio buy budget today: EUR{remaining_portfolio_cap:.2f} / EUR{MAX_PORTFOLIO_BUY_PER_DAY:.0f}")
    if portfolio_blocks:
        lines.append("")
        lines.append("ALL BUYING BLOCKED TODAY:")
        for b in portfolio_blocks:
            lines.append(f"  - {b}")

    lines.append("")
    lines.append("NOTE: earnings-date and bid/ask spread filters are NOT enforced (no free data "
                 "source wired up). See TRADING_RULES.md > Known Implementation Gaps.")
    lines.append("")

    watchlist = load_watchlist()
    would_buy_total = 0.0

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
        decisions = evaluate_ticker(row, position, snap, bull_market, conn, today)

        lines.append(f"{ticker} ({symbol})  price={snap['price']:.2f}  "
                     f"position=EUR{(position['quantity']*position['currentPrice']) if position else 0:.2f}")

        if portfolio_blocks:
            lines.append("    (skipped - portfolio-level block above)")
            continue

        remaining_stock_cap = MAX_STOCK_BUY_PER_DAY - trade_db.daily_stock_buy_total(conn, ticker, today)
        decisions_by_rule = {d["rule"]: d for d in decisions}

        for rule_key in RULE_PRECEDENCE:
            d = decisions_by_rule[rule_key]
            if not d["fired"]:
                lines.append(f"    {d['label']}: no (" + "; ".join(d["blocks"]) + ")")
                continue

            amount = min(d["base_amount"] * buy_multiplier, remaining_stock_cap, remaining_portfolio_cap)
            if amount <= 0:
                lines.append(f"    {d['label']}: WOULD fire, but no budget left "
                             f"(stock cap left EUR{remaining_stock_cap:.2f}, portfolio cap left EUR{remaining_portfolio_cap:.2f})")
                continue

            lines.append(f"    {d['label']}: BUY EUR{amount:.2f} (dry run)")
            trade_db.record_trade(
                conn,
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
                ticker=ticker,
                yahoo_symbol=symbol,
                action="BUY",
                order_size_eur=amount,
                quantity=amount / snap["price"],
                price=snap["price"],
                avg_price=position["averagePrice"] if position else None,
                current_price=snap["price"],
                profit_loss_pct=position and (snap["price"] / position["averagePrice"] - 1) * 100,
                profit_loss_eur=position["ppl"] if position else None,
                spy_regime="bull" if bull_market else "bear",
                sma20=snap["sma20"], ema20=snap["ema20"], sma50=snap["sma50"], sma200=snap["sma200"],
                rsi14=snap["rsi14"], macd=snap["macd"], macd_signal=snap["macd_signal"],
                macd_histogram=snap["macd_histogram"], volume=snap["volume"],
                avg_volume20=snap["avg_volume20"], atr14=snap["atr14"], spread_pct=None,
                reason=d["label"], order_result="DRY_RUN", dry_run=1,
            )
            remaining_stock_cap -= amount
            remaining_portfolio_cap -= amount
            would_buy_total += amount

    lines.append("")
    lines.append(f"Total dry-run buys today: EUR{would_buy_total:.2f}")

    conn.close()
    report = "\n".join(lines)
    print(report)


if __name__ == "__main__":
    main()
