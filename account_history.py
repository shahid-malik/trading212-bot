#!/usr/bin/env python3
"""Analyse your REAL Trading212 trade history: tag every past fill with the same
technical + macro factors as backtest.py (SPY regime, VIX, oil, yields, dollar,
gold, inflation proxy / CPI, geopolitical events from events.csv), rebuild your
positions, and report how your results varied by factor.

Two input options:
  python3 account_history.py --csv export.csv   # Trading212 app: History > Export (CSV)
  python3 account_history.py                    # pull filled orders from the API (.env keys)

Factors are taken as of the last CLOSE BEFORE each trade, i.e. what you could
have known when you placed it. Saves to account_trades.db (trade_factors,
positions - same layout as backtest.db) and account_reports/<timestamp>/.

Positions use average-cost accounting per ticker; a position "closes" when its
quantity returns to zero. Amounts are in each instrument's price currency as
reported by Trading212; FX conversion isn't applied.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import os
import sqlite3
import sys
import time
from pathlib import Path

import factors
import market_data
import t212_portfolio as t212

HERE = Path(__file__).parent
DB_PATH = HERE / "account_trades.db"
REPORTS_DIR = HERE / "account_reports"


def watchlist_map() -> dict[str, str]:
    with (HERE / "watchlist.csv").open(newline="") as f:
        return {r["t212_ticker"]: r["yahoo_symbol"].strip() for r in csv.DictReader(f) if r.get("yahoo_symbol")}


def guess_yahoo(ticker: str, wl: dict[str, str]) -> str:
    """T212 id (AAPL_US_EQ) or plain ticker from the CSV export (AAPL) -> Yahoo symbol."""
    if ticker in wl:
        return wl[ticker]
    by_plain = {t.split("_")[0]: s for t, s in wl.items()}
    if ticker in by_plain:
        return by_plain[ticker]
    if ticker.endswith("_US_EQ"):
        return ticker[:-len("_US_EQ")]
    return ticker.split("_")[0]


def num(v) -> float | None:
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else None
    except ValueError:
        return None


def fills_from_csv(path: Path) -> list[dict]:
    """Trading212 history export: Action, Time, Ticker, No. of shares, Price / share, ..."""
    out = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            action = (r.get("Action") or "").lower()
            if "buy" not in action and "sell" not in action:
                continue  # deposits, dividends, interest, ...
            qty, price = num(r.get("No. of shares")), num(r.get("Price / share"))
            if not qty or not price:
                continue
            out.append({"time": (r.get("Time") or "")[:19].replace(" ", "T"), "ticker": r.get("Ticker") or "",
                        "side": "SELL" if "sell" in action else "BUY", "qty": abs(qty), "price": price,
                        "reason": r.get("Action")})
    return out


def fills_from_api(auth: str, base: str) -> list[dict]:
    """Filled orders from /equity/history/orders, following nextPagePath. Handles
    both the flat order schema and the newer {order, fill} schema."""
    out, path = [], "/equity/history/orders?limit=50"
    while path:
        data = t212.api_get(base, path, auth)
        for item in data.get("items") or []:
            order = item.get("order") or item
            fill = item.get("fill") or item
            status = (order.get("status") or "").upper()
            if status and status != "FILLED":
                continue
            qty = num(fill.get("quantity")) or num(order.get("filledQuantity")) or num(fill.get("filledQuantity"))
            price = num(fill.get("price")) or num(order.get("fillPrice")) or num(fill.get("fillPrice"))
            if not qty or not price:
                continue
            side = (order.get("side") or "").upper()
            if side not in ("BUY", "SELL"):
                ordered = num(order.get("orderedQuantity")) or num(order.get("quantity")) or qty
                side = "SELL" if (ordered or 0) < 0 or qty < 0 else "BUY"
            when = (fill.get("filledAt") or order.get("dateExecuted") or order.get("dateModified")
                    or order.get("createdAt") or order.get("dateCreated") or "")
            ticker = order.get("ticker") or (order.get("instrument") or {}).get("ticker", "")
            out.append({"time": when[:19], "ticker": ticker, "side": side, "qty": abs(qty), "price": price,
                        "reason": order.get("type")})
        nxt = data.get("nextPagePath")
        path = nxt.replace("/api/v0", "") if nxt else None
        if path:
            time.sleep(10)  # history endpoint allows ~6 requests/minute
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, help="Trading212 history export CSV (otherwise uses the API)")
    ap.add_argument("--demo", action="store_true", help="Use the demo/practice API")
    args = ap.parse_args()

    if args.csv:
        raw = fills_from_csv(args.csv)
    else:
        t212.load_dotenv(HERE / ".env")
        key, secret = os.environ.get("T212_API_KEY"), os.environ.get("T212_SECRET_KEY")
        if not key or not secret:
            sys.exit("Set T212_API_KEY / T212_SECRET_KEY in .env, or pass --csv with an app history export.")
        demo = args.demo or os.environ.get("T212_ENV", "live").lower() == "demo"
        raw = fills_from_api(t212.basic_auth_header(key, secret), t212.DEMO_BASE if demo else t212.LIVE_BASE)
    raw = sorted((f for f in raw if f["time"]), key=lambda f: f["time"])
    if not raw:
        sys.exit("No buy/sell fills found.")
    print(f"{len(raw)} fills from {raw[0]['time'][:10]} to {raw[-1]['time'][:10]}")

    wl = watchlist_map()
    bars: dict[str, dict] = {}
    for sym in sorted({guess_yahoo(f["ticker"], wl) for f in raw}):
        try:
            b = market_data.fetch_daily_bars(sym, "10y")
            bars[sym] = {**b, "day": [factors.ts_to_date(t) for t in b["dates"]]}
        except market_data.MarketDataError as e:
            print(f"  no price history for {sym} ({e}) - its trades get macro factors only")
        time.sleep(0.2)
    macro = factors.MacroContext(range_="10y")

    fills = []
    for f in raw:
        sym = guess_yahoo(f["ticker"], wl)
        prior = (dt.date.fromisoformat(f["time"][:10]) - dt.timedelta(days=1)).isoformat()
        snap = {}
        b = bars.get(sym)
        if b:
            i = bisect.bisect_right(b["day"], prior)
            if i >= 30:
                snap = market_data.indicators_from_bars(b["close"][:i], b["high"][:i], b["low"][:i], b["volume"][:i])
        fills.append({"trade_id": None, "date": f["time"], "ticker": f["ticker"], "symbol": sym, "side": f["side"],
                      "reason": f["reason"], "qty": f["qty"], "price_filled": f["price"],
                      "amount": f["qty"] * f["price"], "confidence_pct": None,
                      **{c: snap.get(c) for c in factors.STOCK_COLS},
                      **{c: v for c, v in macro.snapshot(prior).items() if c in factors.MACRO_COLS}})

    last_prices = {}
    for f in fills:
        b = bars.get(f["symbol"])
        if b:
            last_prices[f["ticker"]] = b["close"][-1]
    positions = factors.build_positions(fills, last_prices, dt.date.today().isoformat())

    closed = [p for p in positions if not p["open_at_end"]]
    still_open = [p for p in positions if p["open_at_end"]]
    print(f"\nPositions: {len(closed)} closed, {len(still_open)} open")
    if closed:
        wins = sum(1 for p in closed if p["realized_pnl"] > 0)
        print(f"Closed: win rate {wins / len(closed) * 100:.1f}%, realized P&L {sum(p['realized_pnl'] for p in closed):+.2f}")
    if still_open:
        print(f"Open: unrealized P&L {sum(p['realized_pnl'] for p in still_open):+.2f}")
    print()
    print("\n".join(factors.factor_report(positions)))

    conn = sqlite3.connect(DB_PATH)
    factors.save_factor_tables(conn, fills, positions)
    conn.close()
    out_dir = REPORTS_DIR / time.strftime("%Y-%m-%dT%H-%M-%S")
    factors.write_csvs(out_dir, fills, positions)
    print(f"\nSaved {len(fills)} fills / {len(positions)} positions to {DB_PATH.name} and {out_dir.relative_to(HERE)}/")


if __name__ == "__main__":
    main()
