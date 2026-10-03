"""Free, no-key market data + technical indicators via Yahoo Finance's public chart API.

This is an undocumented endpoint Yahoo serves for its own website; it can change or
rate-limit without notice. Good enough for a personal daily screener, not for anything
production-grade or high-frequency.
"""

from __future__ import annotations

import datetime as dt
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request

_HEADERS = {"User-Agent": "Mozilla/5.0"}


class MarketDataError(Exception):
    pass


def fetch_daily_bars(symbol: str, range_: str = "6mo") -> dict:
    """Returns {"dates": [...], "open": [...], "high": [...], "low": [...],
    "close": [...], "volume": [...]} with None-gaps dropped."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range={range_}"
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise MarketDataError(f"{symbol}: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise MarketDataError(f"{symbol}: {e.reason}") from e

    result = data.get("chart", {}).get("result")
    if not result:
        err = data.get("chart", {}).get("error", {})
        raise MarketDataError(f"{symbol}: {err.get('description', 'no data returned')}")

    r = result[0]
    timestamps = r.get("timestamp") or []
    quote = r["indicators"]["quote"][0]

    bars = {"dates": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
    for i, ts in enumerate(timestamps):
        c = quote["close"][i]
        if c is None:
            continue
        bars["dates"].append(ts)
        bars["open"].append(quote["open"][i])
        bars["high"].append(quote["high"][i])
        bars["low"].append(quote["low"][i])
        bars["close"].append(c)
        bars["volume"].append(quote["volume"][i] or 0)

    if len(bars["close"]) < 30:
        raise MarketDataError(f"{symbol}: only {len(bars['close'])} usable bars, need >=30")

    return bars


# quoteSummary (earnings dates) needs a session cookie + "crumb" token, unlike the
# chart endpoint. Fetched lazily once per process and reused for every ticker.
_cookie_opener: urllib.request.OpenerDirector | None = None
_crumb: str | None = None


def _yahoo_session() -> tuple[urllib.request.OpenerDirector, str]:
    global _cookie_opener, _crumb
    if _cookie_opener is not None and _crumb:
        return _cookie_opener, _crumb
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = list(_HEADERS.items())
    try:
        # fc.yahoo.com answers 404 but sets the session cookie, which is all we need.
        opener.open("https://fc.yahoo.com", timeout=20)
    except urllib.error.HTTPError:
        pass
    except urllib.error.URLError as e:
        raise MarketDataError(f"earnings: cookie fetch failed: {e.reason}") from e
    try:
        with opener.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=20) as resp:
            crumb = resp.read().decode().strip()
    except urllib.error.HTTPError as e:
        raise MarketDataError(f"earnings: crumb fetch HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise MarketDataError(f"earnings: crumb fetch failed: {e.reason}") from e
    if not crumb or "<" in crumb:
        raise MarketDataError("earnings: Yahoo returned no usable crumb")
    _cookie_opener, _crumb = opener, crumb
    return opener, crumb


def fetch_next_earnings(symbol: str, today: dt.date | None = None) -> dict | None:
    """Next scheduled earnings date from Yahoo's calendarEvents module.

    Returns {"date": date, "estimated": bool} or None if Yahoo lists no upcoming
    date (common for ETFs). "estimated" is True when Yahoo marks the date as an
    estimate or only gives a date range (the earliest day of the range is used,
    which is the conservative choice for a "no buy before earnings" filter).
    Raises MarketDataError when the date can't be fetched at all - callers must
    treat that as "unverified", never as "no earnings soon".
    """
    today = today or dt.date.today()
    opener, crumb = _yahoo_session()
    url = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(symbol)}"
           f"?modules=calendarEvents&crumb={urllib.parse.quote(crumb)}")
    try:
        with opener.open(url, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise MarketDataError(f"{symbol}: earnings HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise MarketDataError(f"{symbol}: earnings fetch failed: {e.reason}") from e

    result = (data.get("quoteSummary") or {}).get("result")
    if not result:
        err = (data.get("quoteSummary") or {}).get("error") or {}
        raise MarketDataError(f"{symbol}: earnings {err.get('description', 'no data returned')}")

    earnings = (result[0].get("calendarEvents") or {}).get("earnings") or {}
    dates = sorted(
        dt.datetime.fromtimestamp(d["raw"], tz=dt.timezone.utc).date()
        for d in earnings.get("earningsDate") or []
        if isinstance(d, dict) and d.get("raw")
    )
    upcoming = [d for d in dates if d >= today]
    if not upcoming:
        return None
    return {
        "date": upcoming[0],
        "estimated": bool(earnings.get("isEarningsDateEstimate")) or len(upcoming) > 1,
    }


def trading_days_until(target: dt.date, today: dt.date | None = None) -> int:
    """Weekdays from today (exclusive) to target (inclusive); 0 means today.
    Ignores market holidays, same approximation as the bot's cooldowns."""
    today = today or dt.date.today()
    if target <= today:
        return 0
    days, d = 0, today
    while d < target:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def ema_series(values: list[float], period: int) -> list[float | None]:
    """EMA seeded with the SMA of the first `period` values, standard approach."""
    if len(values) < period:
        return [None] * len(values)
    multiplier = 2 / (period + 1)
    series: list[float | None] = [None] * (period - 1)
    series.append(sum(values[:period]) / period)
    for price in values[period:]:
        prev = series[-1]
        series.append((price - prev) * multiplier + prev)
    return series


def ema(values: list[float], period: int) -> float | None:
    series = ema_series(values, period)
    return series[-1] if series else None


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float | None, float | None, float | None]:
    """Returns (macd_line, signal_line, histogram)."""
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    macd_line = [f - s if f is not None and s is not None else None for f, s in zip(ema_fast, ema_slow)]
    valid = [v for v in macd_line if v is not None]
    if len(valid) < signal:
        return None, None, None
    signal_series = ema_series(valid, signal)
    macd_val = valid[-1]
    signal_val = signal_series[-1]
    histogram = macd_val - signal_val if signal_val is not None else None
    return macd_val, signal_val, histogram


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    """Simple (non-Wilder-smoothed) average true range."""
    if len(closes) < period + 1:
        return None
    true_ranges = []
    for i in range(-period, 0):
        high, low, prev_close = highs[i], lows[i], closes[i - 1]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(true_ranges) / period


def n_day_return(closes: list[float], n: int = 10) -> float | None:
    """% change from n bars ago to the latest close in the slice."""
    if len(closes) <= n or closes[-1 - n] == 0:
        return None
    return (closes[-1] / closes[-1 - n] - 1) * 100


def indicators_from_bars(closes: list[float], highs: list[float], lows: list[float], volumes: list[float]) -> dict:
    """Point-in-time indicator computation from a bars slice - the caller decides
    how much history is visible (live code passes everything fetched; backtest.py
    passes closes[:i+1] etc. for day i, so nothing "sees" future data)."""
    macd_val, signal_val, hist_val = macd(closes)
    return {
        "price": closes[-1],
        "sma20": sma(closes, 20),
        "ema20": ema(closes, 20),
        "sma50": sma(closes, 50),
        "sma200": sma(closes, 200),
        "rsi14": rsi(closes, 14),
        "macd": macd_val,
        "macd_signal": signal_val,
        "macd_histogram": hist_val,
        "volume": volumes[-1],
        "avg_volume20": sma(volumes, 20),
        "atr14": atr(highs, lows, closes, 14),
        "prev_20d_high": max(highs[-21:-1]) if len(highs) >= 21 else None,
        "return_10d": n_day_return(closes, 10),
    }


def indicator_snapshot(symbol: str) -> dict:
    """Full indicator set for trade logging: needs ~1y+ of history for SMA200."""
    bars = fetch_daily_bars(symbol, range_="2y")
    return indicators_from_bars(bars["close"], bars["high"], bars["low"], bars["volume"])


def spy_regime_from_closes(closes: list[float]) -> dict:
    """Point-in-time bull market filter per TRADING_RULES.md: SPY > SMA200 AND
    SMA50 > SMA200, computed from whatever closes are visible to the caller."""
    price = closes[-1]
    s50 = sma(closes, 50)
    s200 = sma(closes, 200)
    bull = bool(s50 and s200 and price > s200 and s50 > s200)
    return {"bull_market": bull, "spy_price": price, "spy_sma50": s50, "spy_sma200": s200,
            "spy_return_10d": n_day_return(closes, 10)}


def spy_regime() -> dict:
    """Bull market filter per TRADING_RULES.md: SPY > SMA200 AND SMA50 > SMA200."""
    bars = fetch_daily_bars("SPY", range_="2y")
    return spy_regime_from_closes(bars["close"])
