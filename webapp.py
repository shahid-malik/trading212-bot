#!/usr/bin/env python3
"""Local-only web UI: edit strategy rule values and browse the trade log with
full indicator context. Runs on your machine only (127.0.0.1) - nothing here
talks to Trading212, places orders, or exposes anything to the network.

Usage:
  .venv/bin/python3 webapp.py
  open http://127.0.0.1:5050
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for

import config as botconfig
import trade_db

HERE = Path(__file__).parent
app = Flask(__name__)


@app.route("/")
def home():
    return redirect(url_for("trades"))


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
    conn.close()

    return render_template("trades.html", rows=rows, tickers=tickers,
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
