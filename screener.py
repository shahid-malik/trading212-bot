#!/usr/bin/env python3
"""Score stocks in watchlist.csv for 'buy' technical signals.

IMPORTANT: this is a rule-based technical heuristic, not a prediction. Nothing can
reliably say a stock "will go up next week." The score combines a trend component
(is it in a confirmed uptrend) and a mean-reversion component (is it oversold and
starting to bounce). Treat a high score as "worth a second look," not as advice.

Reads tickers from watchlist.csv (seeded from your Trading212 holdings, edit that
file to add more candidates - only yahoo_symbol is required, t212_ticker/name/notes
are optional).

Usage:
  python3 screener.py                 # print a report
  python3 screener.py --email out.txt # also write an email-ready summary to a file
  python3 screener.py --min-score 50  # change the "candidate" threshold (default 60)
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import market_data

HERE = Path(__file__).parent


def load_watchlist(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    return rows


def trend_score(bars: dict) -> tuple[int, list[str]]:
    closes = bars["close"]
    price = closes[-1]
    sma20 = market_data.sma(closes, 20)
    sma50 = market_data.sma(closes, 50)
    chg5 = (price / closes[-6] - 1) * 100 if len(closes) > 5 else 0

    score, notes = 0, []
    if sma50 and price > sma50:
        score += 20
        notes.append("price above 50d average")
    if sma20 and sma50 and sma20 > sma50:
        score += 15
        notes.append("20d avg above 50d avg (uptrend)")
    if sma20 and price > sma20 and chg5 > 0:
        score += 15
        notes.append(f"up {chg5:.1f}% over last 5 sessions, above 20d avg")
    return score, notes


def reversion_score(bars: dict) -> tuple[int, list[str]]:
    closes = bars["close"]
    volumes = bars["volume"]
    price = closes[-1]
    rsi14 = market_data.rsi(closes, 14)
    low20 = min(closes[-20:])
    avg_vol20 = market_data.sma(volumes, 20)

    score, notes = 0, []
    if rsi14 is not None and rsi14 < 35:
        score += 25
        notes.append(f"RSI14 oversold ({rsi14:.0f})")
    if price <= low20 * 1.05 and closes[-1] > closes[-2]:
        score += 15
        notes.append("near 20d low and bouncing today")
    if avg_vol20 and volumes[-1] > avg_vol20 * 1.3:
        score += 10
        notes.append("volume above average (confirmation)")
    return score, notes


def analyze(symbol: str) -> dict:
    bars = market_data.fetch_daily_bars(symbol)
    t_score, t_notes = trend_score(bars)
    r_score, r_notes = reversion_score(bars)
    total = t_score + r_score
    price = bars["close"][-1]
    prev = bars["close"][-2]
    return {
        "symbol": symbol,
        "price": price,
        "chg_pct": (price / prev - 1) * 100,
        "score": total,
        "notes": t_notes + r_notes,
    }


def label(score: int) -> str:
    if score >= 65:
        return "STRONG CANDIDATE"
    if score >= 45:
        return "WATCH"
    return "no signal"


def build_report(min_score: int = 60) -> str:
    watchlist = load_watchlist(HERE / "watchlist.csv")

    results, skipped = [], []
    for row in watchlist:
        label_ticker = row.get("t212_ticker") or row.get("yahoo_symbol") or "?"
        symbol = (row.get("yahoo_symbol") or "").strip()
        if not symbol:
            skipped.append((label_ticker, row.get("notes") or "no yahoo_symbol set in watchlist.csv"))
            continue
        try:
            results.append(analyze(symbol))
        except market_data.MarketDataError as e:
            skipped.append((label_ticker, str(e)))
        time.sleep(0.3)  # be polite to Yahoo's endpoint

    results.sort(key=lambda r: -r["score"])

    lines = []
    lines.append(f"Trading212 daily screener - {time.strftime('%Y-%m-%d')}")
    lines.append("Rule-based technical heuristic only. Not a prediction, not financial advice.")
    lines.append("")
    for r in results:
        lines.append(f"{r['symbol']:<10} score={r['score']:<3} {label(r['score']):<17} "
                      f"price={r['price']:.2f} ({r['chg_pct']:+.1f}% today)")
        for n in r["notes"]:
            lines.append(f"    - {n}")
    if skipped:
        lines.append("")
        lines.append("Skipped (no data):")
        for ticker, reason in skipped:
            lines.append(f"  {ticker}: {reason}")

    candidates = [r for r in results if r["score"] >= min_score]
    lines.append("")
    if candidates:
        lines.append(f"{len(candidates)} candidate(s) at/above score {min_score}: "
                      + ", ".join(f"{r['symbol']} ({r['score']})" for r in candidates))
    else:
        lines.append(f"No candidates at/above score {min_score} today.")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", metavar="FILE", help="Write an email-ready summary to FILE")
    parser.add_argument("--min-score", type=int, default=60, help="Score threshold to call something a candidate (default 60)")
    args = parser.parse_args()

    report = build_report(args.min_score)
    print(report)

    if args.email:
        Path(args.email).write_text(report + "\n")
        print(f"\nWrote {args.email}", file=sys.stderr)


if __name__ == "__main__":
    main()
