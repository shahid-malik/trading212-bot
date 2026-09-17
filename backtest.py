#!/usr/bin/env python3
"""Backtest TRADING_RULES.md against historical daily bars, reusing
trading_bot.py's own evaluate_buy()/evaluate_exit() so the backtest can never
drift from what the live dry-run bot actually does - only the data source
(historical vs live Trading212) and portfolio bookkeeping (simulated fills vs
real account state) differ.

Writes to an isolated backtest.db (wiped and rebuilt every run, so results are
reproducible) using the same trade_db.py schema as the live bot - reuses
peak_equity()/previous_equity() for drawdown/daily-loss exactly as main() does.

Real limitations - see TRADING_RULES.md > Backtesting Limitations:
  - Survivorship/lookahead bias: watchlist.csv is today's list, applied
    retroactively across the whole backtest window.
  - No transaction costs, slippage, or fill-price modeling (fills at that
    day's close).
  - Free Yahoo daily bars only - no intraday data, no dividends/splits
    verification beyond what Yahoo's own adjusted series provides.
  - Decision-level audit logging (the ~30 named conditions per ticker per
    day) is NOT written here - only executed trades - to keep the database
    a reasonable size over a multi-year simulation.

Usage:
  python3 backtest.py                    # 5y of history, EUR1000 starting capital
  python3 backtest.py --range 10y --capital 2000
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

import market_data
import trade_db
import trading_bot as tb

HERE = Path(__file__).parent
BACKTEST_DB_PATH = HERE / "backtest.db"
WARMUP_DAYS = 200  # SMA200 needs this much history before a ticker is evaluated


def load_watchlist() -> list[tuple[str, str]]:
    with (HERE / "watchlist.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    return [(r["t212_ticker"], r["yahoo_symbol"].strip()) for r in rows if r.get("yahoo_symbol", "").strip()]


def ts_to_date(ts: int) -> str:
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


def fetch_all_bars(symbols: list[str], range_: str) -> dict:
    data = {}
    for sym in symbols:
        try:
            bars = market_data.fetch_daily_bars(sym, range_=range_)
        except market_data.MarketDataError as e:
            print(f"  skipping {sym}: {e}", file=sys.stderr)
            continue
        date_index = {ts_to_date(t): i for i, t in enumerate(bars["dates"])}
        data[sym] = {**bars, "date_index": date_index}
        time.sleep(0.2)
    return data


def max_drawdown_pct(conn) -> float:
    conn.row_factory = None
    rows = conn.execute("SELECT total_equity FROM equity_snapshots ORDER BY date").fetchall()
    peak, worst = 0.0, 0.0
    for (equity,) in rows:
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak * 100)
    return worst


def run_backtest(range_: str, capital: float) -> None:
    watchlist = load_watchlist()
    if not watchlist:
        sys.exit("watchlist.csv has no tickers with a yahoo_symbol set.")

    ticker_by_symbol = {sym: ticker for ticker, sym in watchlist}
    print(f"Fetching {range_} of history for SPY + {len(watchlist)} watchlist ticker(s)...")
    all_symbols = ["SPY"] + [sym for _, sym in watchlist]
    bars_by_symbol = fetch_all_bars(all_symbols, range_)

    spy_bars = bars_by_symbol.get("SPY")
    if not spy_bars:
        sys.exit("Could not fetch SPY data - cannot backtest without the market regime filter.")
    if len(spy_bars["dates"]) <= WARMUP_DAYS:
        sys.exit(f"Not enough history: got {len(spy_bars['dates'])} days, need > {WARMUP_DAYS} for SMA200 warmup. Try --range 10y.")

    if BACKTEST_DB_PATH.exists():
        BACKTEST_DB_PATH.unlink()
    conn = trade_db.get_conn(BACKTEST_DB_PATH)

    cash = capital
    positions: dict[str, dict] = {}  # ticker -> {quantity, avg_price, last_price}
    trade_log: list[dict] = []
    buy_hold_shares: dict[str, float] | None = None
    sim_days = 0

    for i, spy_ts in enumerate(spy_bars["dates"]):
        if i < WARMUP_DAYS:
            continue
        date = ts_to_date(spy_ts)
        sim_days += 1

        regime = market_data.spy_regime_from_closes(spy_bars["close"][: i + 1])
        bull_market = regime["bull_market"]

        invested_value = 0.0
        for ticker, pos in positions.items():
            sym = next(s for t, s in watchlist if t == ticker)
            j = bars_by_symbol[sym]["date_index"].get(date)
            if j is not None:
                pos["last_price"] = bars_by_symbol[sym]["close"][j]
            invested_value += pos["quantity"] * pos["last_price"]
        total_equity = cash + invested_value
        trade_db.record_equity_snapshot(conn, date, cash, invested_value, total_equity)

        if buy_hold_shares is None:
            valid = [(t, s) for t, s in watchlist if s in bars_by_symbol
                     and bars_by_symbol[s]["date_index"].get(date, -1) >= WARMUP_DAYS]
            if valid:
                per_ticker = capital / len(valid)
                buy_hold_shares = {}
                for t, s in valid:
                    j = bars_by_symbol[s]["date_index"][date]
                    buy_hold_shares[s] = per_ticker / bars_by_symbol[s]["close"][j]

        peak = trade_db.peak_equity(conn) or total_equity
        drawdown_pct = (peak - total_equity) / peak * 100 if peak else 0.0
        prev_equity = trade_db.previous_equity(conn, date)
        daily_loss_pct = ((prev_equity - total_equity) / prev_equity * 100) if prev_equity else 0.0

        max_invested_value = total_equity * (tb.MAX_INVESTED_PCT / 100)
        exposure_headroom = max(0.0, max_invested_value - invested_value)
        portfolio_blocked = (drawdown_pct >= tb.DRAWDOWN_STOP_PCT
                              or daily_loss_pct >= tb.DAILY_LOSS_LIMIT_PCT
                              or exposure_headroom <= 0)
        buy_multiplier = 1.0
        if not portfolio_blocked and drawdown_pct >= tb.DRAWDOWN_REDUCE_PCT:
            buy_multiplier = 1.0 - tb.DRAWDOWN_REDUCE_FACTOR
        remaining_portfolio_cap = min(
            tb.MAX_PORTFOLIO_BUY_PER_DAY - trade_db.daily_portfolio_buy_total(conn, date),
            exposure_headroom,
        )

        for ticker, symbol in watchlist:
            sym_bars = bars_by_symbol.get(symbol)
            if not sym_bars:
                continue
            j = sym_bars["date_index"].get(date)
            if j is None or j < WARMUP_DAYS:
                continue

            snap = market_data.indicators_from_bars(
                sym_bars["close"][: j + 1], sym_bars["high"][: j + 1],
                sym_bars["low"][: j + 1], sym_bars["volume"][: j + 1],
            )

            pos = positions.get(ticker)
            position = None
            if pos and pos["quantity"] > 1e-9:
                position = {"quantity": pos["quantity"], "averagePrice": pos["avg_price"],
                            "currentPrice": snap["price"], "ppl": (snap["price"] - pos["avg_price"]) * pos["quantity"]}

            pstate = trade_db.get_position_state(conn, ticker)
            position_closed_today = False

            if position:
                if not pstate["simulated_open"]:
                    real_change = (pstate["last_known_avg_price"] is None or
                                   abs(position["averagePrice"] - pstate["last_known_avg_price"]) > 1e-6)
                    if real_change:
                        trade_db.reopen_position_state(conn, ticker, position["averagePrice"])
                        pstate = trade_db.get_position_state(conn, ticker)
                    else:
                        position_closed_today = True

            if position and not position_closed_today:
                remaining_fraction = 1.0
                for d in tb.evaluate_exit(position, snap, pstate, bull_market):
                    if not d["fired"]:
                        continue
                    frac = remaining_fraction * d["sell_fraction"]
                    qty = position["quantity"] * frac
                    amount = qty * position["currentPrice"]
                    tb.log_sell(conn, ticker, symbol, position, snap, d, qty, amount, bull_market, date)
                    trade_log.append({"ticker": ticker, "side": "SELL", "date": date,
                                       "price": position["currentPrice"], "qty": qty, "amount": amount,
                                       "avg_price": position["averagePrice"], "reason": d["label"]})
                    pos["quantity"] -= qty
                    cash += amount
                    remaining_fraction *= (1 - d["sell_fraction"])

                    if d.get("closes_position"):
                        if d["rule"] == "exit1":
                            until = tb.add_trading_days(datetime.fromisoformat(date), tb.EMERGENCY_STOP_COOLDOWN_DAYS).strftime("%Y-%m-%d")
                            trade_db.mark_simulated_closed(conn, ticker, d["label"], position["averagePrice"])
                            trade_db.set_position_state(conn, ticker, emergency_stop_until=until)
                        else:
                            trade_db.mark_simulated_closed(conn, ticker, d["label"], position["averagePrice"])
                        position_closed_today = True
                        del positions[ticker]
                        break
                    elif d["rule"] == "exit2":
                        trade_db.set_position_state(conn, ticker, bull_profit_lock=1)
                    elif d["rule"] == "exit3":
                        trade_db.set_position_state(conn, ticker, trailing_stop_active=1,
                                                     trailing_stop_highest_price=position["currentPrice"])
                pstate = trade_db.get_position_state(conn, ticker)

            if position_closed_today or portfolio_blocked:
                continue

            remaining_stock_cap = tb.MAX_STOCK_BUY_PER_DAY - trade_db.daily_stock_buy_total(conn, ticker, date)
            buy_decisions = {d["rule"]: d for d in tb.evaluate_buy(ticker, position, snap, bull_market, pstate, conn, date)}
            conf = buy_decisions["confidence"]
            if not conf["fired"]:
                continue

            amount = min(conf["base_amount"] * buy_multiplier, remaining_stock_cap, remaining_portfolio_cap, cash)
            if amount <= 0:
                continue

            qty = amount / snap["price"]
            tb.log_buy(conn, ticker, symbol, position, snap, conf, amount, bull_market, date)
            trade_log.append({"ticker": ticker, "side": "BUY", "date": date, "price": snap["price"],
                               "qty": qty, "amount": amount, "reason": conf["label"]})

            if ticker not in positions:
                positions[ticker] = {"quantity": 0.0, "avg_price": 0.0, "last_price": snap["price"]}
            p = positions[ticker]
            new_qty = p["quantity"] + qty
            p["avg_price"] = (p["avg_price"] * p["quantity"] + snap["price"] * qty) / new_qty if new_qty else snap["price"]
            p["quantity"] = new_qty
            p["last_price"] = snap["price"]
            cash -= amount
            remaining_stock_cap -= amount
            remaining_portfolio_cap -= amount

    # --- Report ---
    final_invested = sum(p["quantity"] * p["last_price"] for p in positions.values())
    final_equity = cash + final_invested
    total_return_pct = (final_equity / capital - 1) * 100
    dd = max_drawdown_pct(conn)

    buy_hold_value = None
    if buy_hold_shares:
        buy_hold_value = 0.0
        for sym, shares in buy_hold_shares.items():
            closes = bars_by_symbol[sym]["close"]
            buy_hold_value += shares * closes[-1]
    buy_hold_return_pct = (buy_hold_value / capital - 1) * 100 if buy_hold_value else None

    sells = [t for t in trade_log if t["side"] == "SELL"]
    wins = [t for t in sells if t["price"] > t["avg_price"]]
    win_rate = (len(wins) / len(sells) * 100) if sells else None

    print()
    print(f"Backtest: {sim_days} trading days simulated ({spy_bars['dates'][WARMUP_DAYS] and ts_to_date(spy_bars['dates'][WARMUP_DAYS])} to {ts_to_date(spy_bars['dates'][-1])})")
    print(f"Starting capital: EUR{capital:,.2f}")
    print(f"Final equity:     EUR{final_equity:,.2f}  ({total_return_pct:+.1f}%)")
    if buy_hold_return_pct is not None:
        print(f"Equal-weight buy & hold over the same window: {buy_hold_return_pct:+.1f}%")
    print(f"Max drawdown:     {dd:.1f}%")
    print(f"Trades: {len([t for t in trade_log if t['side']=='BUY'])} buys, {len(sells)} sells"
          + (f", win rate {win_rate:.0f}%" if win_rate is not None else ""))
    print()
    print("Not financial advice. Free daily-bar backtest with no transaction costs or")
    print("slippage modeled, and today's watchlist applied retroactively - see")
    print("TRADING_RULES.md > Backtesting Limitations before drawing conclusions.")

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--range", default="5y", choices=["1y", "2y", "5y", "10y", "max"],
                         help="How much history to fetch (default 5y; first 200 trading days are warmup, not simulated)")
    parser.add_argument("--capital", type=float, default=1000.0, help="Starting capital (default EUR1000, matches TRADING_RULES.md)")
    args = parser.parse_args()
    run_backtest(args.range, args.capital)


if __name__ == "__main__":
    main()
