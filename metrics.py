"""Performance/accuracy metrics for the trading bot's trade log. Works against
either the live dry-run trades.db or an isolated backtest.db - both share
trade_db.py's schema, so every function here just takes a sqlite3 connection.

These quantify whether the STRATEGY is any good, separate from whether the
CODE correctly implements the strategy (that's what tests/ covers). Nothing
here is a guarantee of future performance - it's a measurement of what the
logged trades actually did.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime


def equity_curve(conn: sqlite3.Connection) -> list[tuple[str, float]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT date, total_equity FROM equity_snapshots ORDER BY date").fetchall()
    return [(r["date"], r["total_equity"]) for r in rows]


def max_drawdown_pct(curve: list[tuple[str, float]]) -> float | None:
    if not curve:
        return None
    peak, worst = 0.0, 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        if peak > 0:
            worst = max(worst, (peak - eq) / peak * 100)
    return worst


def total_return_pct(curve: list[tuple[str, float]]) -> float | None:
    if len(curve) < 2 or curve[0][1] == 0:
        return None
    return (curve[-1][1] / curve[0][1] - 1) * 100


def cagr_pct(curve: list[tuple[str, float]]) -> float | None:
    if len(curve) < 2 or curve[0][1] <= 0:
        return None
    d0 = datetime.strptime(curve[0][0], "%Y-%m-%d")
    d1 = datetime.strptime(curve[-1][0], "%Y-%m-%d")
    years = (d1 - d0).days / 365.25
    if years <= 0:
        return None
    return ((curve[-1][1] / curve[0][1]) ** (1 / years) - 1) * 100


def daily_returns(curve: list[tuple[str, float]]) -> list[float]:
    rets = []
    for i in range(1, len(curve)):
        prev = curve[i - 1][1]
        if prev:
            rets.append(curve[i][1] / prev - 1)
    return rets


def sharpe_ratio(curve: list[tuple[str, float]], risk_free_annual: float = 0.0) -> float | None:
    """Annualized, assuming ~252 trading days/year. Needs >=2 daily returns."""
    rets = daily_returns(curve)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    variance = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(variance)
    if std == 0:
        return None
    daily_rf = risk_free_annual / 252
    return (mean - daily_rf) / std * math.sqrt(252)


def trade_stats(conn: sqlite3.Connection) -> dict:
    """Win rate / profit factor / expectancy from SELL rows' profit_loss_pct.
    Note: profit_loss_pct is the position's P&L% vs average cost at the moment
    of that sale, accurate regardless of partial vs full sells. profit_factor
    and expectancy weight each trade by its order_size_eur as a proxy for
    capital at risk - not exact realized-EUR accounting for partial sells,
    but directionally correct and consistent across trades."""
    conn.row_factory = sqlite3.Row
    sells = conn.execute(
        "SELECT * FROM trades WHERE action='SELL' AND profit_loss_pct IS NOT NULL ORDER BY timestamp"
    ).fetchall()
    buys = conn.execute("SELECT COUNT(*) c FROM trades WHERE action='BUY'").fetchone()["c"]

    if not sells:
        return {"buy_count": buys, "sell_count": 0, "win_rate_pct": None, "profit_factor": None,
                "expectancy_pct": None, "avg_win_pct": None, "avg_loss_pct": None}

    wins = [t for t in sells if t["profit_loss_pct"] > 0]
    losses = [t for t in sells if t["profit_loss_pct"] <= 0]

    win_rate = len(wins) / len(sells) * 100
    avg_win = sum(t["profit_loss_pct"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t["profit_loss_pct"] for t in losses) / len(losses) if losses else 0.0
    expectancy = (len(wins) / len(sells)) * avg_win + (len(losses) / len(sells)) * avg_loss

    gross_profit = sum((t["order_size_eur"] or 0) * (t["profit_loss_pct"] / 100) for t in wins)
    gross_loss = abs(sum((t["order_size_eur"] or 0) * (t["profit_loss_pct"] / 100) for t in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else None)

    return {
        "buy_count": buys, "sell_count": len(sells),
        "win_rate_pct": win_rate, "profit_factor": profit_factor,
        "expectancy_pct": expectancy, "avg_win_pct": avg_win, "avg_loss_pct": avg_loss,
    }


def rule_accuracy(conn: sqlite3.Connection) -> list[dict]:
    """For each of Rule 1-5, among executed BUYs where that rule individually
    fired, what fraction of that ticker's NEXT sell afterward was profitable?
    Answers "which signal actually predicts a winning trade" - separate from
    whether the rule contributed to the confidence score that triggered entry.
    Older trade rows logged before Rule 4/5 existed have NULL for those
    columns, which is falsy and simply excluded - no migration needed."""
    conn.row_factory = sqlite3.Row
    buys = conn.execute(
        "SELECT * FROM trades WHERE action='BUY' AND order_result='DRY_RUN' ORDER BY timestamp"
    ).fetchall()
    all_sells = conn.execute(
        "SELECT ticker, timestamp, profit_loss_pct FROM trades WHERE action='SELL' AND profit_loss_pct IS NOT NULL ORDER BY timestamp"
    ).fetchall()

    results = []
    for rule_key, rule_label in (
        ("rule1_fired", "Rule 1 - Trend"), ("rule2_fired", "Rule 2 - RSI"), ("rule3_fired", "Rule 3 - MACD"),
        ("rule4_fired", "Rule 4 - Volume Confirmation"), ("rule5_fired", "Rule 5 - Short-Term Momentum"),
    ):
        outcomes = []
        for buy in buys:
            if not buy[rule_key]:
                continue
            next_sell = next((s for s in all_sells if s["ticker"] == buy["ticker"] and s["timestamp"] > buy["timestamp"]), None)
            if next_sell is not None:
                outcomes.append(next_sell["profit_loss_pct"] > 0)
        n = len(outcomes)
        accuracy = (sum(outcomes) / n * 100) if n else None
        results.append({"rule": rule_label, "buys_with_signal": n, "next_sell_profitable_pct": accuracy})
    return results


def summary(conn: sqlite3.Connection) -> dict:
    curve = equity_curve(conn)
    return {
        "equity_curve": curve,
        "total_return_pct": total_return_pct(curve),
        "cagr_pct": cagr_pct(curve),
        "max_drawdown_pct": max_drawdown_pct(curve),
        "sharpe_ratio": sharpe_ratio(curve),
        "trades": trade_stats(conn),
        "rule_accuracy": rule_accuracy(conn),
    }
