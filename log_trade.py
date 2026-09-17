#!/usr/bin/env python3
"""Log a trade you just made into trades.db, capturing the full indicator snapshot
(SMA/EMA/SMA50/SMA200/RSI/MACD/histogram/volume/ATR/SPY regime) plus price, average
cost, current price and P&L - for later analysis/optimization of TRADING_RULES.md.

There's no automated execution yet, so run this by hand right after you place a
trade in the Trading212 app.

Usage:
  python3 log_trade.py --ticker MSFT_US_EQ --action BUY --price 495.17 --qty 0.02 \\
      --order-size 10 --reason "Buy Rule 1 - Trend"

  python3 log_trade.py --ticker MSFT_US_EQ --action SELL --price 510.00 --qty 0.01 \\
      --reason "Exit Rule 2 - Bull market profit"
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

import market_data
import t212_portfolio as t212
import trade_db

HERE = Path(__file__).parent


def load_watchlist_row(ticker: str) -> dict | None:
    with (HERE / "watchlist.csv").open(newline="") as f:
        for row in csv.DictReader(f):
            if row.get("t212_ticker") == ticker:
                return row
    return None


def get_position(auth_header: str, ticker: str) -> dict | None:
    positions = t212.fetch_portfolio(t212.LIVE_BASE, auth_header)
    for p in positions:
        if p["ticker"] == ticker:
            return p
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ticker", required=True, help="Trading212 ticker, e.g. MSFT_US_EQ (must be in watchlist.csv)")
    parser.add_argument("--action", required=True, choices=["BUY", "SELL"])
    parser.add_argument("--price", type=float, required=True, help="Execution price for this trade")
    parser.add_argument("--qty", type=float, required=True, help="Quantity traded")
    parser.add_argument("--order-size", type=float, help="EUR amount of this order (default: price * qty)")
    parser.add_argument("--reason", default="", help="Which rule triggered this, e.g. 'Buy Rule 1 - Trend'")
    parser.add_argument("--order-result", default="filled")
    parser.add_argument("--spread-pct", type=float, default=None)
    args = parser.parse_args()

    row = load_watchlist_row(args.ticker)
    symbol = (row or {}).get("yahoo_symbol")
    if not symbol:
        sys.exit(f"Error: {args.ticker} not found in watchlist.csv with a yahoo_symbol set.")

    t212.load_dotenv(HERE / ".env")
    api_key = os.environ.get("T212_API_KEY")
    api_secret = os.environ.get("T212_SECRET_KEY")

    avg_price = current_price = profit_pct = profit_eur = None
    if api_key and api_secret:
        auth_header = t212.basic_auth_header(api_key, api_secret)
        pos = get_position(auth_header, args.ticker)
        if pos:
            avg_price = pos.get("averagePrice")
            current_price = pos.get("currentPrice")
            profit_eur = pos.get("ppl")
            if avg_price:
                profit_pct = (pos.get("currentPrice") / avg_price - 1) * 100
    else:
        print("Warning: T212_API_KEY/SECRET not set, skipping avg_price/current_price/P&L lookup.", file=sys.stderr)

    print(f"Fetching indicators for {symbol}...", file=sys.stderr)
    snap = market_data.indicator_snapshot(symbol)
    regime = market_data.spy_regime()

    order_size = args.order_size if args.order_size is not None else args.price * args.qty

    conn = trade_db.get_conn()
    row_id = trade_db.record_trade(
        conn,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
        ticker=args.ticker,
        yahoo_symbol=symbol,
        action=args.action,
        order_size_eur=order_size,
        quantity=args.qty,
        price=args.price,
        avg_price=avg_price,
        current_price=current_price if current_price is not None else snap["price"],
        profit_loss_pct=profit_pct,
        profit_loss_eur=profit_eur,
        spy_regime="bull" if regime["bull_market"] else "bear",
        sma20=snap["sma20"],
        ema20=snap["ema20"],
        sma50=snap["sma50"],
        sma200=snap["sma200"],
        rsi14=snap["rsi14"],
        macd=snap["macd"],
        macd_signal=snap["macd_signal"],
        macd_histogram=snap["macd_histogram"],
        volume=snap["volume"],
        avg_volume20=snap["avg_volume20"],
        atr14=snap["atr14"],
        spread_pct=args.spread_pct,
        reason=args.reason,
        order_result=args.order_result,
    )
    conn.close()
    print(f"Logged trade id={row_id}: {args.action} {args.ticker} @ {args.price} x {args.qty}")


if __name__ == "__main__":
    main()
