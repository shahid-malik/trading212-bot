"""factors.py, earnings-date helpers and daily_trade_email.py - pure logic with
stubbed data, no network/DB.

Run: .venv/bin/python3 -m unittest discover -s tests -t .
"""

import datetime as dt
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import daily_trade_email as dte
import factors
import market_data as md


def macro_with(series: dict, events=None, cpi=None) -> factors.MacroContext:
    m = factors.MacroContext.__new__(factors.MacroContext)
    m.series, m.missing, m.events, m.cpi, m._cache = series, [], events or [], cpi, {}
    return m


def flat_series(start: dt.date, values: list[float]) -> factors.Series:
    return factors.Series([(start + dt.timedelta(days=i)).isoformat() for i in range(len(values))], values)


class TestSeries(unittest.TestCase):
    def setUp(self):
        self.s = flat_series(dt.date(2025, 1, 1), [float(v) for v in range(1, 31)])

    def test_value_as_of_uses_last_value_on_or_before(self):
        self.assertEqual(self.s.value("2025-01-05"), 5.0)
        self.assertEqual(self.s.value("2030-01-01"), 30.0)
        self.assertIsNone(self.s.value("2024-12-31"))

    def test_lag_and_pct_change(self):
        self.assertEqual(self.s.value("2025-01-10", lag=4), 6.0)
        self.assertAlmostEqual(self.s.pct_change("2025-01-10", 5), (10 / 5 - 1) * 100)
        self.assertIsNone(self.s.pct_change("2025-01-03", 5))

    def test_sma(self):
        self.assertEqual(self.s.sma("2025-01-05", 5), 3.0)
        self.assertIsNone(self.s.sma("2025-01-03", 5))


class TestSnapshot(unittest.TestCase):
    def test_vix_buckets_and_trends(self):
        start = dt.date(2025, 1, 1)
        m = macro_with({
            "vix": flat_series(start, [30.0] * 70),
            "oil": flat_series(start, [float(60 + i) for i in range(70)]),
            "us10y": flat_series(start, [4.0 - i * 0.01 for i in range(70)]),
            "tip": flat_series(start, [100.0 + i for i in range(70)]),
            "ief": flat_series(start, [100.0] * 70),
        })
        snap = m.snapshot("2025-03-05")  # 63 days in: enough for the 60-day inflation-proxy lag
        self.assertEqual(snap["vix_bucket"], "fear")
        self.assertGreater(snap["oil_chg20"], 0)
        self.assertLess(snap["us10y_chg20_bp"], 0)
        self.assertEqual(snap["infl_proxy_trend"], "rising")

    def test_missing_series_give_none_not_crash(self):
        snap = macro_with({}).snapshot("2025-03-01")
        self.assertIsNone(snap["vix"])
        self.assertIsNone(snap["vix_bucket"])
        self.assertIsNone(snap["infl_proxy_trend"])

    def test_event_window_and_days_since(self):
        events = [{"date": "2025-06-13", "category": "geopolitics", "tag": "israel_iran",
                   "description": "", "window_days": 12},
                  {"date": "2025-06-24", "category": "geopolitics", "tag": "israel_iran_ceasefire",
                   "description": "", "window_days": 5}]
        m = macro_with({}, events=events)
        self.assertEqual(m.snapshot("2025-06-20")["events"], "israel_iran")
        self.assertEqual(m.snapshot("2025-06-25")["events"], "israel_iran;israel_iran_ceasefire")
        self.assertIsNone(m.snapshot("2025-08-01")["events"])
        # ceasefire tag doesn't reset "days since a strike"
        self.assertEqual(m.snapshot("2025-06-30")["days_since_israel_iran"], 17)
        self.assertIsNone(m.snapshot("2025-01-01")["days_since_israel_iran"])

    def test_cpi_uses_month_two_back(self):
        m = macro_with({}, cpi={"2025-01": 3.0, "2025-03": 2.5})
        self.assertEqual(m.snapshot("2025-03-15")["cpi_yoy"], 3.0)
        self.assertEqual(macro_with({}, cpi={"2024-11": 2.7}).snapshot("2025-01-10")["cpi_yoy"], 2.7)


class TestBuildPositions(unittest.TestCase):
    def fill(self, date, side, qty, price, ticker="AAA", **extra):
        return {"date": date, "ticker": ticker, "side": side, "qty": qty, "price_filled": price,
                "reason": "test", **extra}

    def test_round_trip_with_partial_sells(self):
        fills = [self.fill("2025-01-01", "BUY", 10, 10.0, vix_bucket="calm"),
                 self.fill("2025-01-05", "BUY", 10, 20.0),
                 self.fill("2025-01-10", "SELL", 5, 30.0),
                 self.fill("2025-01-20", "SELL", 15, 12.0)]
        positions = factors.build_positions(fills, {}, "2025-02-01")
        self.assertEqual(len(positions), 1)
        p = positions[0]
        self.assertEqual(p["cost"], 300.0)
        self.assertAlmostEqual(p["realized_pnl"], 5 * 30 + 15 * 12 - 300)
        self.assertEqual(p["holding_days"], 19)
        self.assertEqual(p["open_at_end"], 0)
        self.assertEqual(p["entry"]["vix_bucket"], "calm")  # factors from the FIRST buy
        self.assertAlmostEqual(fills[2]["realized_pnl"], 5 * (30 - 15))
        self.assertEqual({f["position_id"] for f in fills}, {1})

    def test_open_position_marked_to_last_price(self):
        positions = factors.build_positions([self.fill("2025-01-01", "BUY", 2, 50.0)], {"AAA": 60.0}, "2025-02-01")
        self.assertEqual(positions[0]["open_at_end"], 1)
        self.assertIsNone(positions[0]["close_date"])
        self.assertAlmostEqual(positions[0]["realized_pnl"], 20.0)

    def test_reopen_after_close_is_new_position(self):
        fills = [self.fill("2025-01-01", "BUY", 1, 10.0), self.fill("2025-01-02", "SELL", 1, 11.0),
                 self.fill("2025-01-03", "BUY", 1, 10.0)]
        positions = factors.build_positions(fills, {"AAA": 10.0}, "2025-02-01")
        self.assertEqual([p["position_id"] for p in positions], [1, 2])

    def test_bucket_rows_split_multi_category_events(self):
        fills = [self.fill("2025-01-01", "BUY", 1, 10.0, event_categories="fed;geopolitics", spy_bull=True),
                 self.fill("2025-01-02", "SELL", 1, 12.0)]
        positions = factors.build_positions(fills, {}, "2025-02-01")
        keys = {(name, key) for name, key, _ in factors.bucket_rows(positions)}
        self.assertIn(("Event window", "fed"), keys)
        self.assertIn(("Event window", "geopolitics"), keys)
        self.assertIn(("SPY regime", "bull"), keys)


class TestEarnings(unittest.TestCase):
    def test_trading_days_until_skips_weekends(self):
        fri = dt.date(2026, 10, 9)
        self.assertEqual(md.trading_days_until(dt.date(2026, 10, 12), fri), 1)
        self.assertEqual(md.trading_days_until(fri, fri), 0)
        self.assertEqual(md.trading_days_until(dt.date(2026, 10, 1), fri), 0)

    def _fetch(self, payload, today):
        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass
        opener = mock.Mock()
        opener.open.return_value = Resp(json.dumps(payload).encode())
        with mock.patch.object(md, "_yahoo_session", return_value=(opener, "crumb")):
            return md.fetch_next_earnings("AAA", today=today)

    @staticmethod
    def ts(d):
        return int(dt.datetime(d.year, d.month, d.day, 20, tzinfo=dt.timezone.utc).timestamp())

    def test_picks_earliest_upcoming_and_flags_ranges(self):
        today = dt.date(2026, 10, 3)
        payload = {"quoteSummary": {"result": [{"calendarEvents": {"earnings": {
            "earningsDate": [{"raw": self.ts(dt.date(2026, 10, 30))}, {"raw": self.ts(dt.date(2026, 11, 3))}],
            "isEarningsDateEstimate": False}}}]}}
        self.assertEqual(self._fetch(payload, today), {"date": dt.date(2026, 10, 30), "estimated": True})

    def test_no_upcoming_date_returns_none(self):
        payload = {"quoteSummary": {"result": [{"calendarEvents": {"earnings": {
            "earningsDate": [{"raw": self.ts(dt.date(2026, 7, 30))}]}}}]}}
        self.assertIsNone(self._fetch(payload, dt.date(2026, 10, 3)))

    def test_error_payload_raises(self):
        with self.assertRaises(md.MarketDataError):
            self._fetch({"quoteSummary": {"result": None, "error": {"description": "Not Found"}}}, dt.date(2026, 10, 3))


class TestDailyEmail(unittest.TestCase):
    TRADES = [
        {"action": "BUY", "ticker": "NVDA_US_EQ", "order_size_eur": 30.0, "price": 180.5, "confidence_pct": 86.0},
        {"action": "SELL", "ticker": "TSLA_US_EQ", "order_size_eur": 25.0, "price": 300.0,
         "profit_loss_pct": -10.4, "reason": "Exit Rule 1 - Emergency Stop"},
    ]

    def test_subject_and_sections(self):
        subject, text, body = dte.build_email("2026-10-05", self.TRADES,
                                              {"total_equity": 1000.0, "cash": 400.0, "invested_value": 600.0})
        self.assertEqual(subject, "T212 bot 2026-10-05: 1 buy, 1 sell")
        self.assertIn("NVDA_US_EQ", text)
        self.assertIn("Emergency Stop", text)
        self.assertIn("-10.4%", text)
        self.assertIn("EUR1,000.00", body)

    def test_no_trades(self):
        subject, text, _ = dte.build_email("2026-10-05", [], None)
        self.assertEqual(subject, "T212 bot 2026-10-05: 0 buys, 0 sells")
        self.assertIn("nothing to buy", text)
        self.assertIn("nothing to sell", text)

    def test_report_is_html_escaped(self):
        _, _, body = dte.build_email("2026-10-05", [], None, report="<script>x</script>")
        self.assertNotIn("<script>", body)

    def test_smtp_send(self):
        env = {"SMTP_USER": "me@example.com", "SMTP_PASSWORD": "pw", "EMAIL_TO": "a@x.com, b@x.com"}
        with mock.patch.dict("os.environ", env), mock.patch.object(dte.smtplib, "SMTP") as smtp:
            to = dte.send_smtp("s", "t", "<p>h</p>")
        server = smtp.return_value.__enter__.return_value
        server.login.assert_called_once_with("me@example.com", "pw")
        self.assertEqual(server.sendmail.call_args[0][1], ["a@x.com", "b@x.com"])
        self.assertEqual(to, "a@x.com, b@x.com")

    def test_smtp_requires_credentials(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit):
                dte.send_smtp("s", "t", "h")


if __name__ == "__main__":
    unittest.main()
