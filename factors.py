"""Macro / market-context factors for tagging trades (backtest.py, account_history.py).

Every trade gets a snapshot of the conditions it was made in, so you can later ask
"how did my trades do when VIX was high / oil was rising / right after an
Israel-Iran escalation?" Factors, all as of the close of the given date (no
look-ahead):

  spy_*        - bull/bear regime (same SPY SMA50/SMA200 filter as the bot), SPY vs SMA200
  vix*         - CBOE volatility index level and calm/normal/fear bucket
  oil_*        - WTI crude (CL=F) price and 20-day change
  us10y_*      - US 10-year Treasury yield (^TNX) and 20-day change in basis points
  dxy_chg20    - US dollar index (DX-Y.NYB) 20-day change
  gold_chg20   - gold (GC=F) 20-day change
  infl_proxy_* - inflation-expectations proxy: TIP/IEF ratio 60-day change. TIPS
                 outperforming plain Treasuries means the market is pricing in more
                 inflation. A market proxy, NOT the official CPI figure.
  cpi_yoy      - official US CPI year-on-year % from FRED (only if
                 fred.stlouisfed.org is reachable; None otherwise). Uses the CPI
                 month two months back, since CPI is published ~2 weeks after
                 month end.
  events / days_since_israel_iran - tags from events.csv active on that date

Data comes from the same free Yahoo endpoint as market_data.py.
"""

from __future__ import annotations

import bisect
import csv
import datetime as dt
import json
import urllib.error
import urllib.request
from pathlib import Path

import market_data

HERE = Path(__file__).parent

MACRO_SYMBOLS = {
    "spy": "SPY",
    "vix": "^VIX",
    "oil": "CL=F",
    "us10y": "^TNX",
    "dxy": "DX-Y.NYB",
    "gold": "GC=F",
    "tip": "TIP",
    "ief": "IEF",
}


def ts_to_date(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).date().isoformat()


class Series:
    """Daily closes keyed by ISO date, with as-of lookup (last value on/before a date)."""

    def __init__(self, dates: list[str], closes: list[float]):
        self.dates = dates
        self.closes = closes

    @classmethod
    def from_bars(cls, bars: dict) -> "Series":
        return cls([ts_to_date(t) for t in bars["dates"]], list(bars["close"]))

    def idx(self, date: str) -> int | None:
        i = bisect.bisect_right(self.dates, date) - 1
        return i if i >= 0 else None

    def value(self, date: str, lag: int = 0) -> float | None:
        i = self.idx(date)
        if i is None or i - lag < 0:
            return None
        return self.closes[i - lag]

    def pct_change(self, date: str, days: int) -> float | None:
        now, then = self.value(date), self.value(date, days)
        if now is None or not then:
            return None
        return (now / then - 1) * 100

    def sma(self, date: str, window: int) -> float | None:
        i = self.idx(date)
        if i is None or i + 1 < window:
            return None
        return sum(self.closes[i + 1 - window:i + 1]) / window


def load_events(path: Path = HERE / "events.csv") -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["window_days"] = int(r.get("window_days") or 10)
    return sorted(rows, key=lambda r: r["date"])


def fetch_cpi_yoy() -> dict[str, float] | None:
    """{'YYYY-MM': yoy_pct} from FRED's public CSV, or None if unreachable."""
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode()
    except (urllib.error.URLError, TimeoutError):
        return None
    levels = {}
    for line in text.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) == 2 and parts[1] not in ("", "."):
            levels[parts[0][:7]] = float(parts[1])
    out = {}
    for month, level in levels.items():
        y, m = int(month[:4]), int(month[5:])
        prev = levels.get(f"{y - 1:04d}-{m:02d}")
        if prev:
            out[month] = (level / prev - 1) * 100
    return out or None


class MacroContext:
    """Loads macro series once, then answers snapshot(date) quickly."""

    def __init__(self, range_: str = "5y", events_path: Path = HERE / "events.csv", verbose: bool = True):
        self.series: dict[str, Series] = {}
        self.missing: list[str] = []
        for key, sym in MACRO_SYMBOLS.items():
            try:
                self.series[key] = Series.from_bars(market_data.fetch_daily_bars(sym, range_=range_))
            except market_data.MarketDataError as e:
                self.missing.append(f"{key} ({sym}): {e}")
        self.events = load_events(events_path)
        self.cpi = fetch_cpi_yoy()
        self._cache: dict[str, dict] = {}
        if verbose:
            for m in self.missing:
                print(f"  macro factor unavailable: {m}")
            if self.cpi is None:
                print("  official CPI unavailable (fred.stlouisfed.org unreachable) - using TIP/IEF proxy only")

    def _get(self, key: str) -> Series | None:
        return self.series.get(key)

    def active_events(self, date: str) -> list[dict]:
        d = dt.date.fromisoformat(date)
        out = []
        for e in self.events:
            start = dt.date.fromisoformat(e["date"])
            if start <= d <= start + dt.timedelta(days=e["window_days"]):
                out.append(e)
        return out

    def days_since(self, date: str, tag: str) -> int | None:
        d = dt.date.fromisoformat(date)
        past = [dt.date.fromisoformat(e["date"]) for e in self.events
                if e["tag"] == tag and e["date"] <= date]
        return (d - max(past)).days if past else None

    def snapshot(self, date: str) -> dict:
        if date not in self._cache:
            self._cache[date] = self._snapshot(date)
        return self._cache[date]

    def _snapshot(self, date: str) -> dict:
        snap: dict = {"date": date}

        spy = self._get("spy")
        if spy:
            price, s50, s200 = spy.value(date), spy.sma(date, 50), spy.sma(date, 200)
            snap["spy_bull"] = bool(price and s50 and s200 and price > s200 and s50 > s200)
            snap["spy_vs_sma200_pct"] = (price / s200 - 1) * 100 if price and s200 else None
            snap["spy_chg20"] = spy.pct_change(date, 20)

        vix = self._get("vix")
        v = vix.value(date) if vix else None
        snap["vix"] = v
        snap["vix_bucket"] = None if v is None else ("calm" if v < 15 else "normal" if v < 25 else "fear")

        oil = self._get("oil")
        snap["oil"] = oil.value(date) if oil else None
        snap["oil_chg20"] = oil.pct_change(date, 20) if oil else None

        tnx = self._get("us10y")
        y_now = tnx.value(date) if tnx else None
        y_then = tnx.value(date, 20) if tnx else None
        snap["us10y"] = y_now
        snap["us10y_chg20_bp"] = (y_now - y_then) * 100 if y_now is not None and y_then is not None else None

        dxy = self._get("dxy")
        snap["dxy_chg20"] = dxy.pct_change(date, 20) if dxy else None
        gold = self._get("gold")
        snap["gold_chg20"] = gold.pct_change(date, 20) if gold else None

        tip, ief = self._get("tip"), self._get("ief")
        ratio_now = ratio_then = None
        if tip and ief:
            t0, i0, t1, i1 = tip.value(date), ief.value(date), tip.value(date, 60), ief.value(date, 60)
            if t0 and i0 and t1 and i1:
                ratio_now, ratio_then = t0 / i0, t1 / i1
        snap["infl_proxy_chg60"] = (ratio_now / ratio_then - 1) * 100 if ratio_now and ratio_then else None
        snap["infl_proxy_trend"] = (None if snap["infl_proxy_chg60"] is None
                                     else "rising" if snap["infl_proxy_chg60"] > 0 else "falling")

        snap["cpi_yoy"] = None
        if self.cpi:
            d = dt.date.fromisoformat(date)
            m = d.month - 2
            y = d.year + (m - 1) // 12
            m = (m - 1) % 12 + 1
            snap["cpi_yoy"] = self.cpi.get(f"{y:04d}-{m:02d}")

        events = self.active_events(date)
        snap["events"] = ";".join(sorted({e["tag"] for e in events})) or None
        snap["event_categories"] = ";".join(sorted({e["category"] for e in events})) or None
        snap["days_since_israel_iran"] = self.days_since(date, "israel_iran")
        return snap


# --------------------------------------------------------------------------- per-trade factor records
# Shared by backtest.py and account_history.py: every fill gets a flat dict of
# indicators + macro factors, fills are grouped into positions (first buy ->
# quantity back to zero), and results are broken down by the factors at entry.

STOCK_COLS = ["price", "sma20", "ema20", "sma50", "sma200", "rsi14", "macd", "macd_signal",
              "macd_histogram", "volume", "avg_volume20", "atr14", "prev_20d_high", "return_10d"]
MACRO_COLS = ["spy_bull", "spy_vs_sma200_pct", "spy_chg20", "vix", "vix_bucket", "oil", "oil_chg20",
              "us10y", "us10y_chg20_bp", "dxy_chg20", "gold_chg20", "infl_proxy_chg60",
              "infl_proxy_trend", "cpi_yoy", "events", "event_categories", "days_since_israel_iran"]
FILL_COLS = (["trade_id", "date", "ticker", "symbol", "side", "reason", "qty", "price_filled", "amount",
              "realized_pnl", "position_id", "confidence_pct"] + STOCK_COLS + MACRO_COLS)
POSITION_COLS = ["position_id", "ticker", "open_date", "close_date", "holding_days", "n_buys", "cost",
                 "proceeds", "realized_pnl", "return_pct", "exit_reasons", "open_at_end"]
ENTRY_COLS = ["confidence_pct"] + STOCK_COLS + MACRO_COLS


def build_positions(fills: list[dict], last_prices: dict[str, float], today: str) -> list[dict]:
    """Average-cost round trips per ticker from chronologically ordered fills
    (each needs ticker, side, qty, price_filled, date, reason). Mutates fills to
    add realized_pnl and position_id. Positions still open are valued at
    last_prices and flagged open_at_end=1."""
    open_pos: dict[str, dict] = {}
    positions: list[dict] = []
    next_id = 1
    for f in fills:
        po = open_pos.get(f["ticker"])
        f["realized_pnl"] = None
        if f["side"] == "BUY":
            if po is None:
                po = open_pos[f["ticker"]] = {"id": next_id, "qty": 0.0, "avg": 0.0, "cost": 0.0, "proceeds": 0.0,
                                              "n_buys": 0, "open": f["date"], "reasons": [], "entry": f}
                next_id += 1
            amount = f["qty"] * f["price_filled"]
            po["avg"] = (po["avg"] * po["qty"] + amount) / (po["qty"] + f["qty"])
            po["qty"] += f["qty"]
            po["cost"] += amount
            po["n_buys"] += 1
        elif po is not None:
            qty = min(f["qty"], po["qty"])
            f["realized_pnl"] = qty * (f["price_filled"] - po["avg"])
            po["qty"] -= qty
            po["proceeds"] += qty * f["price_filled"]
            po["reasons"].append(f.get("reason") or "sell")
        f["position_id"] = po["id"] if po else None
        if po is not None and po["qty"] <= 1e-9 * max(1.0, po["cost"]):
            positions.append(_position_row(po, f["ticker"], f["date"], po["proceeds"], open_at_end=0))
            del open_pos[f["ticker"]]
    for ticker, po in open_pos.items():
        value = po["qty"] * last_prices.get(ticker, po["avg"])
        row = _position_row(po, ticker, today, po["proceeds"] + value, open_at_end=1)
        row["close_date"] = None
        positions.append(row)
    return positions


def _position_row(po: dict, ticker: str, end_date: str, value_out: float, open_at_end: int) -> dict:
    pnl = value_out - po["cost"]
    return {"position_id": po["id"], "ticker": ticker, "open_date": po["open"], "close_date": end_date,
            "holding_days": (dt.date.fromisoformat(end_date[:10]) - dt.date.fromisoformat(po["open"][:10])).days,
            "n_buys": po["n_buys"], "cost": po["cost"], "proceeds": po["proceeds"], "realized_pnl": pnl,
            "return_pct": pnl / po["cost"] * 100 if po["cost"] else 0.0,
            "exit_reasons": "; ".join(po["reasons"]) or "(still open)", "open_at_end": open_at_end,
            "entry": po["entry"]}


def bucket_rows(positions: list[dict]) -> list[tuple[str, str, list[dict]]]:
    """(factor, bucket, positions) groups, keyed on the factors at entry."""
    def group(name, fn):
        groups: dict[str, list] = {}
        for p in positions:
            key = fn(p["entry"])
            if key is None:
                continue
            for k in (key if isinstance(key, list) else [key]):
                groups.setdefault(k, []).append(p)
        return [(name, k, v) for k, v in sorted(groups.items())]

    def sign(v, up="rising", down="falling"):
        return None if v is None else (up if v > 0 else down)

    rows = []
    rows += group("SPY regime", lambda e: None if e.get("spy_bull") is None else ("bull" if e["spy_bull"] else "bear"))
    rows += group("VIX", lambda e: e.get("vix_bucket"))
    rows += group("Oil 20d", lambda e: sign(e.get("oil_chg20")))
    rows += group("10y yield 20d", lambda e: sign(e.get("us10y_chg20_bp")))
    rows += group("Dollar 20d", lambda e: sign(e.get("dxy_chg20")))
    rows += group("Inflation proxy", lambda e: e.get("infl_proxy_trend"))
    rows += group("CPI YoY", lambda e: None if e.get("cpi_yoy") is None else
                  ("<3%" if e["cpi_yoy"] < 3 else "3-5%" if e["cpi_yoy"] < 5 else ">5%"))
    rows += group("Event window", lambda e: (e.get("event_categories") or "none").split(";"))
    rows += group("Israel-Iran", lambda e: None if e.get("days_since_israel_iran") is None else
                  ("<=30d after strike" if e["days_since_israel_iran"] <= 30 else ">30d after strike"))
    rows += group("Confidence", lambda e: None if e.get("confidence_pct") is None else
                  f"{int(e['confidence_pct'] // 10 * 10)}-{int(e['confidence_pct'] // 10 * 10) + 9}%")
    rows += group("Ticker", lambda e: e.get("ticker"))
    return rows


def factor_report(positions: list[dict]) -> list[str]:
    lines = ["Results by factor at entry (closed + still-open positions):",
             f"  {'factor':<16} {'bucket':<28} {'n':>4} {'win%':>6} {'avg ret%':>9} {'P&L':>10}"]
    for name, key, ps in bucket_rows(positions):
        wins = sum(1 for p in ps if p["realized_pnl"] > 0)
        lines.append(f"  {name:<16} {key[:28]:<28} {len(ps):>4} {wins / len(ps) * 100:>5.0f}% "
                     f"{sum(p['return_pct'] for p in ps) / len(ps):>+9.2f} {sum(p['realized_pnl'] for p in ps):>+10.2f}")
    return lines


def save_factor_tables(conn, fills: list[dict], positions: list[dict]) -> None:
    """trade_factors (one row per fill, joinable to trades.id via trade_id) and
    positions (one row per round trip with entry_* factors)."""
    conn.executescript(f"""
        DROP TABLE IF EXISTS trade_factors;
        DROP TABLE IF EXISTS positions;
        CREATE TABLE trade_factors ({', '.join(FILL_COLS)}, factors_json TEXT);
        CREATE TABLE positions ({', '.join(POSITION_COLS)}, {', '.join('entry_' + c for c in ENTRY_COLS)});
    """)
    conn.executemany(f"INSERT INTO trade_factors VALUES ({','.join('?' * (len(FILL_COLS) + 1))})",
                     [[_sql(f.get(c)) for c in FILL_COLS] + [json.dumps(f, default=str)] for f in fills])
    conn.executemany(f"INSERT INTO positions VALUES ({','.join('?' * (len(POSITION_COLS) + len(ENTRY_COLS)))})",
                     [[_sql(p.get(c)) for c in POSITION_COLS] + [_sql(p["entry"].get(c)) for c in ENTRY_COLS]
                      for p in positions])
    conn.commit()


def _sql(v):
    return int(v) if isinstance(v, bool) else v


def write_csvs(out_dir: Path, fills: list[dict], positions: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "fills.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FILL_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(fills)
    with (out_dir / "positions.csv").open("w", newline="") as f:
        cols = POSITION_COLS + ["entry_" + c for c in ENTRY_COLS]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows({**p, **{"entry_" + c: p["entry"].get(c) for c in ENTRY_COLS}} for p in positions)
