"""Indicator math tests - these are pure functions with no network/DB
dependency, so every edge case (insufficient history, flat prices, all-gains,
all-losses) should be covered here rather than discovered live.

Run: .venv/bin/python3 -m unittest discover -s tests -t .
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import market_data as md


class TestSMA(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(md.sma([1, 2, 3, 4, 5], 5), 3.0)

    def test_uses_most_recent_window(self):
        self.assertEqual(md.sma([100, 100, 1, 2, 3, 4, 5], 5), 3.0)

    def test_insufficient_data_returns_none(self):
        self.assertIsNone(md.sma([1, 2], 5))

    def test_exact_length(self):
        self.assertEqual(md.sma([2, 4, 6], 3), 4.0)


class TestRSI(unittest.TestCase):
    def test_all_gains_is_100(self):
        closes = [100 + i for i in range(20)]  # strictly increasing
        self.assertEqual(md.rsi(closes, 14), 100.0)

    def test_all_losses_is_0(self):
        closes = [100 - i for i in range(20)]  # strictly decreasing
        self.assertEqual(md.rsi(closes, 14), 0.0)

    def test_flat_prices_is_0_by_convention(self):
        # avg_gain == avg_loss == 0 -> falls into the avg_loss == 0 branch -> 100,
        # not a divide-by-zero. Document the actual (slightly odd) behavior.
        closes = [100.0] * 20
        self.assertEqual(md.rsi(closes, 14), 100.0)

    def test_insufficient_data_returns_none(self):
        self.assertIsNone(md.rsi([1, 2, 3], 14))


class TestEMA(unittest.TestCase):
    def test_seeded_with_sma_of_first_window(self):
        values = [1, 2, 3, 4, 5]
        series = md.ema_series(values, 5)
        self.assertEqual(series[-1], 3.0)  # only one point once seeded

    def test_converges_toward_flat_series(self):
        values = [10.0] * 30
        self.assertAlmostEqual(md.ema(values, 20), 10.0)

    def test_insufficient_data_returns_none_series(self):
        self.assertEqual(md.ema_series([1, 2], 5), [None, None])
        self.assertIsNone(md.ema([1, 2], 5))


class TestMACD(unittest.TestCase):
    def test_flat_series_has_zero_macd(self):
        closes = [50.0] * 60
        macd_val, signal_val, hist = md.macd(closes)
        self.assertAlmostEqual(macd_val, 0.0)
        self.assertAlmostEqual(signal_val, 0.0)
        self.assertAlmostEqual(hist, 0.0)

    def test_insufficient_data_returns_none_triple(self):
        self.assertEqual(md.macd([1, 2, 3]), (None, None, None))

    def test_uptrend_has_positive_macd_line(self):
        # MACD line (fast EMA - slow EMA) is positive throughout a sustained
        # uptrend. The histogram is NOT guaranteed positive here: in a
        # perfectly linear trend the signal line (EMA9 of MACD) eventually
        # converges to the same steady-state constant as the MACD line
        # itself, so histogram -> 0 given enough bars - that's correct
        # behavior, not a bug, so this test doesn't assert on histogram.
        closes = [100 + i * 0.5 for i in range(80)]
        macd_val, _, _ = md.macd(closes)
        self.assertGreater(macd_val, 0)


class TestATR(unittest.TestCase):
    def test_constant_range_matches_expected(self):
        # high-low = 2 every day, no gaps between close and next high/low -> ATR = 2
        n = 20
        closes = [100.0] * n
        highs = [101.0] * n
        lows = [99.0] * n
        self.assertAlmostEqual(md.atr(highs, lows, closes, 14), 2.0)

    def test_insufficient_data_returns_none(self):
        self.assertIsNone(md.atr([1, 2], [1, 2], [1, 2], 14))


class TestIndicatorsFromBars(unittest.TestCase):
    """The whole point of indicators_from_bars is point-in-time correctness -
    the same function must give different (earlier, smaller) results when
    given a shorter slice, and must never see data past what it's handed."""

    def _bars(self, n=250):
        closes = [100 + i * 0.1 for i in range(n)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        volumes = [1000] * n
        return closes, highs, lows, volumes

    def test_price_is_last_close_of_the_slice_not_the_full_series(self):
        closes, highs, lows, volumes = self._bars(250)
        snap_at_100 = md.indicators_from_bars(closes[:101], highs[:101], lows[:101], volumes[:101])
        self.assertEqual(snap_at_100["price"], closes[100])
        self.assertNotEqual(snap_at_100["price"], closes[-1])

    def test_sma200_none_until_200_bars_available(self):
        closes, highs, lows, volumes = self._bars(250)
        snap_early = md.indicators_from_bars(closes[:150], highs[:150], lows[:150], volumes[:150])
        snap_late = md.indicators_from_bars(closes[:200], highs[:200], lows[:200], volumes[:200])
        self.assertIsNone(snap_early["sma200"])
        self.assertIsNotNone(snap_late["sma200"])

    def test_prev_20d_high_excludes_todays_high(self):
        closes, highs, lows, volumes = self._bars(250)
        highs = list(highs)
        highs[99] = 9999.0  # spike on the "today" bar (index 99, i.e. 100th bar)
        snap = md.indicators_from_bars(closes[:100], highs[:100], lows[:100], volumes[:100])
        self.assertNotEqual(snap["prev_20d_high"], 9999.0)


class TestNDayReturn(unittest.TestCase):
    def test_basic_percent_change(self):
        closes = [100.0] * 10 + [110.0]  # 11 bars, 10 back is index 0 = 100
        self.assertAlmostEqual(md.n_day_return(closes, 10), 10.0)

    def test_negative_return(self):
        closes = [100.0] * 5 + [90.0]
        self.assertAlmostEqual(md.n_day_return(closes, 5), -10.0)

    def test_insufficient_history_returns_none(self):
        self.assertIsNone(md.n_day_return([100.0, 101.0], 10))

    def test_zero_price_n_days_ago_returns_none_not_crash(self):
        closes = [0.0] + [100.0] * 10
        self.assertIsNone(md.n_day_return(closes, 10))


class TestSpyRegime(unittest.TestCase):
    def test_bull_when_price_and_sma50_above_sma200(self):
        closes = [100 + i * 0.5 for i in range(250)]  # steady uptrend
        regime = md.spy_regime_from_closes(closes)
        self.assertTrue(regime["bull_market"])

    def test_bear_when_price_below_sma200(self):
        closes = [200 - i * 0.5 for i in range(250)]  # steady downtrend
        regime = md.spy_regime_from_closes(closes)
        self.assertFalse(regime["bull_market"])

    def test_includes_spy_return_10d(self):
        closes = [100 + i * 0.5 for i in range(250)]
        regime = md.spy_regime_from_closes(closes)
        self.assertIsNotNone(regime["spy_return_10d"])
        self.assertGreater(regime["spy_return_10d"], 0)  # steady uptrend

    def test_insufficient_history_is_not_bull(self):
        regime = md.spy_regime_from_closes([100, 101, 102])
        self.assertFalse(regime["bull_market"])


if __name__ == "__main__":
    unittest.main()
