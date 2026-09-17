#!/usr/bin/env python3
"""Local-only web UI: edit strategy rule values and browse the trade log with
full indicator context. Runs on your machine only (127.0.0.1) - nothing here
talks to Trading212, places orders, or exposes anything to the network.

Usage:
  .venv/bin/python3 webapp.py
  open http://127.0.0.1:5050
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for

import config as botconfig
import metrics
import trade_db

HERE = Path(__file__).parent
WATCHLIST_PATH = HERE / "watchlist.csv"
WATCHLIST_FIELDS = ["t212_ticker", "yahoo_symbol", "name", "notes"]
BACKTEST_DB_PATH = HERE / "backtest.db"
app = Flask(__name__)


def equity_curve_svg(curve: list[tuple[str, float]], width: int = 760, height: int = 220) -> str:
    """Minimal server-rendered line chart - no JS/chart library needed."""
    if len(curve) < 2:
        return '<p class="muted">Not enough equity history to chart yet.</p>'

    pad = 30
    values = [v for _, v in curve]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(curve)

    def x_of(i: int) -> float:
        return pad + (i / (n - 1)) * (width - 2 * pad)

    def y_of(v: float) -> float:
        return height - pad - ((v - lo) / span) * (height - 2 * pad)

    points = " ".join(f"{x_of(i):.1f},{y_of(v):.1f}" for i, (_, v) in enumerate(curve))
    start_date, end_date = curve[0][0], curve[-1][0]

    return f'''<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" aria-label="Equity curve">
      <line x1="{pad}" y1="{height-pad}" x2="{width-pad}" y2="{height-pad}" stroke="#ccc" stroke-width="1"/>
      <polyline points="{points}" fill="none" stroke="#14213d" stroke-width="1.6"/>
      <text x="{pad}" y="{height-8}" font-size="11" fill="#888">{start_date}</text>
      <text x="{width-pad}" y="{height-8}" font-size="11" fill="#888" text-anchor="end">{end_date}</text>
      <text x="{pad}" y="14" font-size="11" fill="#888">EUR{hi:,.0f}</text>
      <text x="{pad}" y="{height-pad-4}" font-size="11" fill="#888">EUR{lo:,.0f}</text>
    </svg>'''


def load_watchlist_rows() -> list[dict]:
    with WATCHLIST_PATH.open(newline="") as f:
        return list(csv.DictReader(f))


def write_watchlist_rows(rows: list[dict]) -> None:
    with WATCHLIST_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=WATCHLIST_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (row.get(k) or "") for k in WATCHLIST_FIELDS})


@app.route("/")
def home():
    return redirect(url_for("dashboard"))


@app.route("/rules", methods=["GET", "POST"])
def rules():
    saved = False
    error = None
    if request.method == "POST":
        try:
            cfg = {key: request.form[key] for group in botconfig.FIELD_GROUPS for key, _ in group[1]}
            botconfig.save_config(cfg)
            saved = True
        except (KeyError, ValueError) as e:
            error = f"Couldn't save: {e}"

    cfg = botconfig.load_config()
    return render_template("rules.html", groups=botconfig.FIELD_GROUPS, cfg=cfg, saved=saved, error=error)


@app.route("/trades")
def trades():
    conn = trade_db.get_conn()
    conn.row_factory = sqlite3.Row
    ticker = request.args.get("ticker", "")
    action = request.args.get("action", "")
    run_type = request.args.get("run_type", "")

    query = "SELECT * FROM trades WHERE 1=1"
    params: list = []
    if ticker:
        query += " AND ticker = ?"
        params.append(ticker)
    if action in ("BUY", "SELL"):
        query += " AND action = ?"
        params.append(action)
    if run_type == "dry":
        query += " AND dry_run = 1"
    elif run_type == "real":
        query += " AND dry_run = 0"
    query += " ORDER BY timestamp DESC LIMIT 500"

    rows = conn.execute(query, params).fetchall()
    tickers = [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM trades ORDER BY ticker").fetchall()]

    # Full rule breakdown per row: every gate/rule evaluated for that ticker in
    # that same bot run, not just the one that produced this trade.
    all_rules = {}
    for row in rows:
        if not row["run_id"]:
            continue
        decisions = trade_db.decisions_for_run(conn, row["run_id"], row["ticker"])
        all_rules[row["id"]] = [
            {
                "label": d["label"],
                "fired": bool(d["fired"]),
                "conditions": json.loads(d["conditions_json"]) if d["conditions_json"] else [],
            }
            for d in decisions
        ]
    conn.close()

    return render_template("trades.html", rows=rows, tickers=tickers, all_rules=all_rules,
                            ticker=ticker, action=action, run_type=run_type)


@app.route("/trades/<int:trade_id>")
def trade_detail(trade_id: int):
    conn = trade_db.get_conn()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    conn.close()
    if row is None:
        return "Trade not found", 404
    return render_template("trade_detail.html", row=row)


@app.route("/decisions")
def decisions():
    conn = trade_db.get_conn()
    ticker = request.args.get("ticker", "")
    side = request.args.get("side", "")
    fired_only = request.args.get("fired_only") == "1"
    executed_only = request.args.get("executed_only") == "1"

    rows = trade_db.recent_decisions(conn, ticker=ticker, side=side,
                                      fired_only=fired_only, executed_only=executed_only, limit=1000)
    tickers = [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM decisions ORDER BY ticker").fetchall()]
    conn.close()

    return render_template("decisions.html", rows=rows, tickers=tickers, ticker=ticker, side=side,
                            fired_only=fired_only, executed_only=executed_only)


@app.route("/watchlist")
def watchlist():
    rows = load_watchlist_rows()
    return render_template("watchlist.html", rows=rows,
                            error=request.args.get("error"),
                            added=request.args.get("added"),
                            removed=request.args.get("removed"))


@app.route("/watchlist/add", methods=["POST"])
def watchlist_add():
    ticker = request.form.get("t212_ticker", "").strip()
    yahoo_symbol = request.form.get("yahoo_symbol", "").strip()
    name = request.form.get("name", "").strip()
    notes = request.form.get("notes", "").strip()

    if not ticker or not yahoo_symbol:
        return redirect(url_for("watchlist", error="Both Trading212 ticker and Yahoo symbol are required."))

    rows = load_watchlist_rows()
    if any(r["t212_ticker"] == ticker for r in rows):
        return redirect(url_for("watchlist", error=f"{ticker} is already on the watchlist."))

    rows.append({"t212_ticker": ticker, "yahoo_symbol": yahoo_symbol, "name": name, "notes": notes})
    write_watchlist_rows(rows)
    return redirect(url_for("watchlist", added=ticker))


@app.route("/watchlist/remove", methods=["POST"])
def watchlist_remove():
    ticker = request.form.get("t212_ticker", "")
    rows = [r for r in load_watchlist_rows() if r["t212_ticker"] != ticker]
    write_watchlist_rows(rows)
    return redirect(url_for("watchlist", removed=ticker))


@app.route("/dashboard")
def dashboard():
    source = request.args.get("source", "live")
    db_path = BACKTEST_DB_PATH if source == "backtest" else trade_db.DB_PATH

    if not db_path.exists():
        return render_template("dashboard.html", source=source, available=False, svg=None, s=None)

    conn = trade_db.get_conn(db_path)
    s = metrics.summary(conn)
    conn.close()

    svg = equity_curve_svg(s["equity_curve"])
    return render_template("dashboard.html", source=source, available=True, svg=svg, s=s)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
