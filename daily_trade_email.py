#!/usr/bin/env python3
"""Email the day's bot decisions: what it would BUY and SELL, plus an equity summary.

Reads the trades trading_bot.py logged to trades.db for the given day (default
today) - so run it after the bot. run_bot.sh does this automatically, which
means the existing weekday launchd job (com.t212.bot) sends it every morning.

The bot is dry-run only, so the email is a to-do list: nothing has been
executed in your Trading212 account.

Sending (pick one):
  - SMTP (default): set in .env
        SMTP_USER=you@gmail.com
        SMTP_PASSWORD=<Gmail App Password, not your normal password>
        EMAIL_TO=you@gmail.com            # optional, defaults to SMTP_USER
        SMTP_HOST=smtp.gmail.com          # optional
        SMTP_PORT=587                     # optional
    Gmail App Passwords: Google Account > Security > 2-Step Verification > App passwords.
  - --draft: create a Gmail draft instead via gmail_draft.py (its OAuth setup).

Usage:
  python3 daily_trade_email.py                  # today's trades, send
  python3 daily_trade_email.py --print          # show the email, don't send
  python3 daily_trade_email.py --date 2026-10-05
  python3 daily_trade_email.py --report reports/bot_latest.txt   # append the full bot report
"""

from __future__ import annotations

import argparse
import html
import os
import smtplib
import sqlite3
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import t212_portfolio as t212
import trade_db

HERE = Path(__file__).parent
LOG_PATH = HERE / "daily_trade_email.log"


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def load_day(conn: sqlite3.Connection, day: str) -> tuple[list[dict], dict | None]:
    conn.row_factory = sqlite3.Row
    trades = [dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE substr(timestamp, 1, 10) = ? ORDER BY action, ticker", (day,))]
    snap = conn.execute("SELECT * FROM equity_snapshots WHERE date <= ? ORDER BY date DESC LIMIT 1", (day,)).fetchone()
    return trades, dict(snap) if snap else None


def _money(v) -> str:
    return f"EUR{v:,.2f}" if v is not None else "-"


def _pct(v) -> str:
    return f"{v:+.1f}%" if v is not None else "-"


def build_email(day: str, trades: list[dict], equity: dict | None, report: str | None = None) -> tuple[str, str, str]:
    """Returns (subject, plain_text, html)."""
    buys = [t for t in trades if t["action"] == "BUY"]
    sells = [t for t in trades if t["action"] == "SELL"]
    subject = f"T212 bot {day}: {len(buys)} buy{'s' * (len(buys) != 1)}, {len(sells)} sell{'s' * (len(sells) != 1)}"

    buy_rows = [(t["ticker"], _money(t["order_size_eur"]), f"{t['price']:.2f}" if t["price"] else "-",
                 f"{t['confidence_pct']:.0f}%" if t.get("confidence_pct") is not None else "-") for t in buys]
    sell_rows = [(t["ticker"], _money(t["order_size_eur"]), f"{t['price']:.2f}" if t["price"] else "-",
                  _pct(t.get("profit_loss_pct")), t.get("reason") or "") for t in sells]

    lines = [f"Trading212 bot decisions for {day}",
             "DRY RUN - nothing was executed. Place these in the Trading212 app yourself if you agree.", ""]
    if equity:
        lines.append(f"Equity {_money(equity['total_equity'])}  (cash {_money(equity['cash'])}, "
                     f"invested {_money(equity['invested_value'])})")
        lines.append("")
    lines.append(f"BUY ({len(buys)})")
    lines += [f"  {r[0]:<14} {r[1]:>12}  at {r[2]:>9}  confidence {r[3]}" for r in buy_rows] or ["  nothing to buy"]
    lines.append("")
    lines.append(f"SELL ({len(sells)})")
    lines += [f"  {r[0]:<14} {r[1]:>12}  at {r[2]:>9}  P/L {r[3]:>7}  {r[4]}" for r in sell_rows] or ["  nothing to sell"]
    if report:
        lines += ["", "-" * 60, "Full bot report:", "", report]
    lines += ["", "Not financial advice."]
    text = "\n".join(lines)

    def table(headers, rows, empty):
        if not rows:
            return f"<p style='color:#666'>{empty}</p>"
        head = "".join(f"<th style='text-align:left;padding:4px 10px;border-bottom:1px solid #ccc'>{h}</th>" for h in headers)
        body = "".join("<tr>" + "".join(f"<td style='padding:4px 10px'>{html.escape(str(c))}</td>" for c in r) + "</tr>"
                       for r in rows)
        return f"<table style='border-collapse:collapse'><tr>{head}</tr>{body}</table>"

    eq = (f"<p>Equity <b>{_money(equity['total_equity'])}</b> (cash {_money(equity['cash'])}, "
          f"invested {_money(equity['invested_value'])})</p>") if equity else ""
    rep = f"<hr><p>Full bot report:</p><pre style='font-size:12px'>{html.escape(report)}</pre>" if report else ""
    body_html = f"""<div style="font-family:Arial,sans-serif;font-size:14px">
<h2 style="margin:0 0 4px">Trading212 bot decisions for {day}</h2>
<p style="color:#b00">DRY RUN - nothing was executed. Place these in the Trading212 app yourself if you agree.</p>
{eq}
<h3 style="color:#0a7d32">BUY ({len(buys)})</h3>
{table(["Ticker", "Amount", "Price", "Confidence"], buy_rows, "Nothing to buy today.")}
<h3 style="color:#b3261e">SELL ({len(sells)})</h3>
{table(["Ticker", "Amount", "Price", "P/L", "Rule"], sell_rows, "Nothing to sell today.")}
{rep}
<p style="color:#666;font-size:12px">Not financial advice.</p></div>"""
    return subject, text, body_html


def send_smtp(subject: str, text: str, body_html: str) -> str:
    user, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    if not user or not password:
        raise SystemExit("Set SMTP_USER and SMTP_PASSWORD in .env (or use --draft / --print).")
    to = os.environ.get("EMAIL_TO") or user
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(body_html, "html"))
    with smtplib.SMTP(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "587")),
                      timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    return to


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"), help="Day to report (default today)")
    ap.add_argument("--report", type=Path, help="Append this bot report file to the email")
    ap.add_argument("--print", dest="print_only", action="store_true", help="Print the email instead of sending")
    ap.add_argument("--draft", action="store_true", help="Create a Gmail draft (gmail_draft.py) instead of SMTP")
    args = ap.parse_args()
    t212.load_dotenv(HERE / ".env")

    conn = trade_db.get_conn()
    trades, equity = load_day(conn, args.date)
    conn.close()
    report = args.report.read_text() if args.report and args.report.exists() else None
    subject, text, body_html = build_email(args.date, trades, equity, report)

    if args.print_only:
        print(f"Subject: {subject}\n\n{text}")
        return
    try:
        if args.draft:
            import gmail_draft
            to = os.environ.get("EMAIL_TO") or os.environ.get("SMTP_USER")
            if not to:
                raise SystemExit("Set EMAIL_TO in .env for --draft.")
            draft = gmail_draft.create_draft(to, subject, text)
            log(f"OK - draft id={draft.get('id')}: {subject}")
        else:
            to = send_smtp(subject, text, body_html)
            log(f"OK - sent to {to}: {subject}")
    except SystemExit:
        raise
    except Exception as e:
        log(f"email failed: {e!r}")
        sys.exit(1)


if __name__ == "__main__":
    main()
