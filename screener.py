#!/usr/bin/env python3
"""Score stocks in watchlist.csv for 'buy' technical signals.

IMPORTANT: this is a rule-based technical heuristic, not a prediction. Nothing can
reliably say a stock "will go up next week." The score combines a trend component
(is it in a confirmed uptrend) and a mean-reversion component (is it oversold and
starting to bounce). Treat a high score as "worth a second look," not as advice.

Each ticker also gets its next earnings date. Per TRADING_RULES.md there is no new
buy within 3 trading days of earnings, so tickers inside that window are dropped from
the candidate list (they're still scored and shown). If the date can't be fetched the
ticker is marked "earnings unverified" - check it manually before buying.

Reads tickers from watchlist.csv (seeded from your Trading212 holdings, edit that
file to add more candidates - only yahoo_symbol is required, t212_ticker/name/notes
are optional).

Usage:
  python3 screener.py                 # print a report
  python3 screener.py --email out.txt # also write an email-ready summary to a file
  python3 screener.py --min-score 50  # change the "candidate" threshold (default 60)
  python3 screener.py --earnings-days 5  # widen the pre-earnings blackout (default 3)
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


def earnings_info(symbol: str) -> dict:
    """{"date", "estimated", "trading_days", "error"} - error set when unverified."""
    try:
        nxt = market_data.fetch_next_earnings(symbol)
    except market_data.MarketDataError as e:
        return {"date": None, "estimated": False, "trading_days": None, "error": str(e)}
    if nxt is None:
        return {"date": None, "estimated": False, "trading_days": None, "error": None}
    return {**nxt, "trading_days": market_data.trading_days_until(nxt["date"]), "error": None}


def earnings_text(e: dict) -> str:
    if e["error"]:
        return "earnings: UNVERIFIED (could not fetch date)"
    if e["date"] is None:
        return "earnings: none scheduled"
    est = ", estimated" if e["estimated"] else ""
    n = e["trading_days"]
    return f"earnings: {e['date'].isoformat()} (in {n} trading day{'' if n == 1 else 's'}{est})"


def in_blackout(e: dict, window: int) -> bool:
    return e["trading_days"] is not None and e["trading_days"] <= window


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
        "earnings": earnings_info(symbol),
    }


def label(score: int) -> str:
    if score >= 65:
        return "STRONG CANDIDATE"
    if score >= 45:
        return "WATCH"
    return "no signal"


def build_report(min_score: int = 60, earnings_days: int = 3) -> str:
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
    lines.append(f"Earnings blackout: no new buy within {earnings_days} trading days of earnings.")
    lines.append("")
    for r in results:
        e = r["earnings"]
        flag = "  <-- EARNINGS BLACKOUT" if in_blackout(e, earnings_days) else ""
        lines.append(f"{r['symbol']:<10} score={r['score']:<3} {label(r['score']):<17} "
                      f"price={r['price']:.2f} ({r['chg_pct']:+.1f}% today)")
        lines.append(f"    {earnings_text(e)}{flag}")
        for n in r["notes"]:
            lines.append(f"    - {n}")
    if skipped:
        lines.append("")
        lines.append("Skipped (no data):")
        for ticker, reason in skipped:
            lines.append(f"  {ticker}: {reason}")

    scored = [r for r in results if r["score"] >= min_score]
    candidates = [r for r in scored if not in_blackout(r["earnings"], earnings_days)]
    blacked_out = [r for r in scored if in_blackout(r["earnings"], earnings_days)]
    lines.append("")
    if candidates:
        lines.append(f"{len(candidates)} candidate(s) at/above score {min_score}: "
                      + ", ".join(f"{r['symbol']} ({r['score']})"
                                  + (" [earnings unverified]" if r["earnings"]["error"] else "")
                                  for r in candidates))
    else:
        lines.append(f"No candidates at/above score {min_score} today.")
    if blacked_out:
        lines.append(f"Excluded (earnings within {earnings_days} trading days): "
                      + ", ".join(f"{r['symbol']} ({r['earnings']['date'].isoformat()})" for r in blacked_out))
    unverified = [r["symbol"] for r in results if r["earnings"]["error"]]
    if unverified:
        lines.append("Earnings date unverified (check manually before buying): " + ", ".join(unverified))

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", metavar="FILE", help="Write an email-ready summary to FILE")
    parser.add_argument("--min-score", type=int, default=60, help="Score threshold to call something a candidate (default 60)")
    parser.add_argument("--earnings-days", type=int, default=3,
                        help="Exclude candidates with earnings within this many trading days (default 3)")
    args = parser.parse_args()

    report = build_report(args.min_score, args.earnings_days)
    print(report)

    if args.email:
        Path(args.email).write_text(report + "\n")
        print(f"\nWrote {args.email}", file=sys.stderr)


if __name__ == "__main__":
    main()
