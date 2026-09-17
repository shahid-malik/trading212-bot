"""SQLite trade log: one row per BUY/SELL, with the full indicator snapshot from
TRADING_RULES.md's "Log every decision" list, plus price/avg/current/P&L fields.
Kept as plain sqlite3 (stdlib only) so `sqlite3 trades.db` or pandas.read_sql can
query it directly for analysis - no ORM, no extra dependencies.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "trades.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    ticker TEXT NOT NULL,
    yahoo_symbol TEXT,
    action TEXT NOT NULL CHECK(action IN ('BUY','SELL')),
    order_size_eur REAL,
    quantity REAL,
    price REAL,
    avg_price REAL,
    current_price REAL,
    profit_loss_pct REAL,
    profit_loss_eur REAL,
    spy_regime TEXT,
    sma20 REAL,
    ema20 REAL,
    sma50 REAL,
    sma200 REAL,
    rsi14 REAL,
    macd REAL,
    macd_signal REAL,
    macd_histogram REAL,
    volume REAL,
    avg_volume20 REAL,
    atr14 REAL,
    spread_pct REAL,
    reason TEXT,
    order_result TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    date TEXT PRIMARY KEY,
    cash REAL,
    invested_value REAL,
    total_equity REAL,
    recorded_at TEXT
);

CREATE TABLE IF NOT EXISTS position_state (
    ticker TEXT PRIMARY KEY,
    bull_profit_lock INTEGER NOT NULL DEFAULT 0,
    trailing_stop_active INTEGER NOT NULL DEFAULT 0,
    trailing_stop_highest_price REAL,
    emergency_stop_until TEXT,
    simulated_open INTEGER NOT NULL DEFAULT 1,
    last_known_avg_price REAL,
    close_reason TEXT,
    updated_at TEXT
);
"""


_MIGRATIONS = [
    "ALTER TABLE trades ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE position_state ADD COLUMN simulated_open INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE position_state ADD COLUMN last_known_avg_price REAL",
    "ALTER TABLE position_state ADD COLUMN close_reason TEXT",
]


def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists (pre-existing db created before this field was added)
    conn.commit()
    return conn


def record_trade(conn: sqlite3.Connection, **fields) -> int:
    columns = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    cur = conn.execute(f"INSERT INTO trades ({columns}) VALUES ({placeholders})", list(fields.values()))
    conn.commit()
    return cur.lastrowid


def all_trades(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM trades ORDER BY timestamp").fetchall()


def record_equity_snapshot(conn: sqlite3.Connection, date: str, cash: float, invested_value: float, total_equity: float) -> None:
    conn.execute(
        """INSERT INTO equity_snapshots (date, cash, invested_value, total_equity, recorded_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET
             cash=excluded.cash, invested_value=excluded.invested_value,
             total_equity=excluded.total_equity, recorded_at=excluded.recorded_at""",
        (date, cash, invested_value, total_equity, time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    conn.commit()


def peak_equity(conn: sqlite3.Connection) -> float | None:
    row = conn.execute("SELECT MAX(total_equity) FROM equity_snapshots").fetchone()
    return row[0] if row else None


def previous_equity(conn: sqlite3.Connection, before_date: str) -> float | None:
    row = conn.execute(
        "SELECT total_equity FROM equity_snapshots WHERE date < ? ORDER BY date DESC LIMIT 1",
        (before_date,),
    ).fetchone()
    return row[0] if row else None


def daily_stock_buy_total(conn: sqlite3.Connection, ticker: str, date: str) -> float:
    row = conn.execute(
        """SELECT COALESCE(SUM(order_size_eur), 0) FROM trades
           WHERE ticker = ? AND action = 'BUY' AND timestamp LIKE ?""",
        (ticker, f"{date}%"),
    ).fetchone()
    return row[0] or 0.0


def daily_portfolio_buy_total(conn: sqlite3.Connection, date: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(order_size_eur), 0) FROM trades WHERE action = 'BUY' AND timestamp LIKE ?",
        (f"{date}%",),
    ).fetchone()
    return row[0] or 0.0


def last_buy_timestamp(conn: sqlite3.Connection, ticker: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(timestamp) FROM trades WHERE ticker = ? AND action = 'BUY'",
        (ticker,),
    ).fetchone()
    return row[0] if row else None


def get_position_state(conn: sqlite3.Connection, ticker: str) -> dict:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM position_state WHERE ticker = ?", (ticker,)).fetchone()
    if row:
        return dict(row)
    return {
        "ticker": ticker,
        "bull_profit_lock": 0,
        "trailing_stop_active": 0,
        "trailing_stop_highest_price": None,
        "emergency_stop_until": None,
        "simulated_open": 1,
        "last_known_avg_price": None,
        "close_reason": None,
        "updated_at": None,
    }


def set_position_state(conn: sqlite3.Connection, ticker: str, **fields) -> None:
    fields["ticker"] = ticker
    fields["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    columns = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    updates = ", ".join(f"{k}=excluded.{k}" for k in fields if k != "ticker")
    conn.execute(
        f"""INSERT INTO position_state ({columns}) VALUES ({placeholders})
            ON CONFLICT(ticker) DO UPDATE SET {updates}""",
        list(fields.values()),
    )
    conn.commit()


def mark_simulated_closed(conn: sqlite3.Connection, ticker: str, reason: str, avg_price: float | None) -> None:
    """The dry-run bot decided to fully sell, but the real position is untouched.
    Marks this ticker as closed *in the simulation* so exit rules stop re-firing
    on it every run - real prices don't reset just because we "sold" on paper.
    Records avg_price as the baseline to detect a real trade happening later.
    See reopen_position_state() for when this gets cleared."""
    set_position_state(conn, ticker, simulated_open=0, close_reason=reason, last_known_avg_price=avg_price,
                        bull_profit_lock=0, trailing_stop_active=0, trailing_stop_highest_price=None)


def reopen_position_state(conn: sqlite3.Connection, ticker: str, avg_price: float | None) -> None:
    """A real change was detected (position gone, or average price moved - i.e.
    you actually traded this ticker for real) - treat it as a fresh instance:
    profit lock and trailing stop reset per TRADING_RULES.md resolved definitions."""
    set_position_state(conn, ticker, simulated_open=1, close_reason=None, last_known_avg_price=avg_price,
                        bull_profit_lock=0, trailing_stop_active=0, trailing_stop_highest_price=None)
