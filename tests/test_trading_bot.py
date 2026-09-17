"""Tests for the rule-evaluation logic itself: confidence-score math, exit-rule
priority ordering, and cooldown day-counting. Uses an isolated in-memory
trade_db connection per test so nothing touches the real trades.db.

Run: .venv/bin/python3 -m unittest discover -s tests -t .
"""

import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import trade_db
import trading_bot as tb


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(trade_db.SCHEMA)
    return conn


def full_snap(**overrides) -> dict:
    """A snapshot where every Buy Rule 1/2/3 condition is true by default -
    override individual fields to make specific conditions fail."""
    base = {
        "price": 100.0, "ema20": 95.0, "sma50": 90.0, "sma200": 80.0, "sma20": 98.0,
        "rsi14": 60.0, "macd": 2.0, "macd_signal": 1.0, "macd_histogram": 1.0,
        "volume": 1_000_000, "avg_volume20": 800_000, "atr14": 2.0, "prev_20d_high": 99.0,
    }
    base.update(overrides)
    return base


class TestEvaluateBuyConfidence(unittest.TestCase):
    def setUp(self):
        self.conn = make_conn()
        self.pstate = trade_db.get_position_state(self.conn, "TEST")

    def test_all_conditions_true_is_100_percent_confidence(self):
        decisions = tb.evaluate_buy("TEST", None, full_snap(), True, self.pstate, self.conn, "2026-01-01")
        confidence = decisions[-1]
        self.assertEqual(confidence["rule"], "confidence")
        self.assertAlmostEqual(confidence["confidence_pct"], 100.0)
        self.assertTrue(confidence["fired"])

    def test_confidence_below_threshold_does_not_fire(self):
        # Kill the shared trend conditions (used by all 3 rules). Rule 1 also
        # has 2 always-true "not enforced" conditions (earnings/spread - no
        # free data source, see TRADING_RULES.md), so it doesn't go to 0%:
        # Rule1: 2/5 (not-enforced only) = 40%*40=16. Rule2: 1/4 (RSI only)
        # = 25%*25=6.25. Rule3: 2/5 (MACD only) = 40%*35=14. Total = 36.25%.
        snap = full_snap(ema20=200.0, sma50=200.0, sma200=200.0)  # price 100 < all of these
        decisions = tb.evaluate_buy("TEST", None, snap, True, self.pstate, self.conn, "2026-01-01")
        confidence = decisions[-1]
        self.assertAlmostEqual(confidence["confidence_pct"], 36.25, places=1)
        self.assertLess(confidence["confidence_pct"], tb.CONFIDENCE_THRESHOLD_PCT)
        self.assertFalse(confidence["fired"])

    def test_bear_market_blocks_even_at_100_percent_confidence(self):
        decisions = tb.evaluate_buy("TEST", None, full_snap(), False, self.pstate, self.conn, "2026-01-01")
        confidence = decisions[-1]
        self.assertAlmostEqual(confidence["confidence_pct"], 100.0)
        self.assertFalse(confidence["fired"])  # gate failure overrides a perfect score
        self.assertIn("Gate - Bull Market Required", confidence["blocks"])

    def test_position_at_max_blocks_via_gate_not_score(self):
        position = {"quantity": 1.0, "averagePrice": 100.0, "currentPrice": 100.0, "ppl": 0.0}
        decisions = tb.evaluate_buy("TEST", position, full_snap(), True, self.pstate, self.conn, "2026-01-01")
        gates = next(d for d in decisions if d["rule"] == "gates")
        self.assertFalse(gates["fired"])
        self.assertTrue(any("Position Below Max" in b for b in gates["blocks"]))

    def test_losing_position_blocks_buy(self):
        position = {"quantity": 1.0, "averagePrice": 100.0, "currentPrice": 90.0, "ppl": -10.0}
        decisions = tb.evaluate_buy("TEST", position, full_snap(price=90.0), True, self.pstate, self.conn, "2026-01-01")
        gates = next(d for d in decisions if d["rule"] == "gates")
        self.assertFalse(gates["fired"])
        self.assertTrue(any("Not Losing" in b for b in gates["blocks"]))

    def test_rule_components_independent_of_confidence_gate(self):
        # Bear market blocks the confidence decision but Rule 1/2/3's own
        # condition sets are unaffected - they're purely technical, not gated.
        decisions = tb.evaluate_buy("TEST", None, full_snap(), False, self.pstate, self.conn, "2026-01-01")
        rule1 = next(d for d in decisions if d["rule"] == "rule1")
        self.assertTrue(rule1["fired"])

    def test_none_indicator_values_fail_safe_not_crash(self):
        snap = full_snap(rsi14=None, macd=None, macd_signal=None, macd_histogram=None, sma200=None)
        decisions = tb.evaluate_buy("TEST", None, snap, True, self.pstate, self.conn, "2026-01-01")
        confidence = decisions[-1]
        self.assertFalse(confidence["fired"])
        self.assertLess(confidence["confidence_pct"], 100.0)


class TestEvaluateExitPriority(unittest.TestCase):
    def setUp(self):
        self.conn = make_conn()

    def _pstate(self, **overrides):
        p = trade_db.get_position_state(self.conn, "TEST")
        p.update(overrides)
        return p

    def test_emergency_stop_overrides_everything_else(self):
        # -8% loss: emergency stop should fire and be the ONLY decision
        # returned, even though other conditions might also look attractive.
        position = {"quantity": 1.0, "averagePrice": 100.0, "currentPrice": 92.0, "ppl": -8.0}
        snap = full_snap(price=92.0)
        decisions = tb.evaluate_exit(position, snap, self._pstate(), True)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["rule"], "exit1")
        self.assertTrue(decisions[0]["fired"])
        self.assertEqual(decisions[0]["sell_fraction"], 1.0)

    def test_small_loss_does_not_trigger_emergency_stop(self):
        position = {"quantity": 1.0, "averagePrice": 100.0, "currentPrice": 95.0, "ppl": -5.0}
        decisions = tb.evaluate_exit(position, full_snap(price=95.0), self._pstate(), True)
        exit1 = decisions[0]
        self.assertEqual(exit1["rule"], "exit1")
        self.assertFalse(exit1["fired"])

    def test_bull_profit_fires_once_then_locked(self):
        position = {"quantity": 1.0, "averagePrice": 100.0, "currentPrice": 104.0, "ppl": 4.0}
        snap = full_snap(price=104.0)
        decisions = tb.evaluate_exit(position, snap, self._pstate(bull_profit_lock=0), True)
        exit2 = next(d for d in decisions if d["rule"] == "exit2")
        self.assertTrue(exit2["fired"])
        self.assertEqual(exit2["sell_fraction"], tb.BULL_PROFIT_SELL_FRACTION)

        # Same conditions, but lock already used -> must not fire again.
        decisions2 = tb.evaluate_exit(position, snap, self._pstate(bull_profit_lock=1), True)
        exit2_locked = next(d for d in decisions2 if d["rule"] == "exit2")
        self.assertFalse(exit2_locked["fired"])

    def test_trailing_stop_only_evaluated_when_armed(self):
        position = {"quantity": 1.0, "averagePrice": 100.0, "currentPrice": 50.0, "ppl": -50.0}
        # -50% would normally trigger emergency stop first - use a smaller
        # move so we can isolate trailing-stop behavior specifically.
        position = {"quantity": 1.0, "averagePrice": 100.0, "currentPrice": 103.0, "ppl": 3.0}
        snap = full_snap(price=103.0, atr14=1.0)

        not_armed = self._pstate(trailing_stop_active=0)
        decisions = tb.evaluate_exit(position, snap, not_armed, True)
        exit4 = next(d for d in decisions if d["rule"] == "exit4")
        self.assertFalse(exit4["fired"])
        self.assertIn(("Exit 4 - Trailing Stop Armed", False), exit4["conditions"])

    def test_trailing_stop_triggers_when_price_drops_below_level(self):
        # Armed at a highest price of 120, ATR14=2 -> trailing level = 120 - 2*2 = 116.
        position = {"quantity": 1.0, "averagePrice": 100.0, "currentPrice": 115.0, "ppl": 15.0}
        snap = full_snap(price=115.0, atr14=2.0)
        armed = self._pstate(trailing_stop_active=1, trailing_stop_highest_price=120.0)
        decisions = tb.evaluate_exit(position, snap, armed, True)
        exit4 = decisions[-1]  # emergency(no)+trailing(fires) -> loop returns after exit4 fires
        self.assertEqual(exit4["rule"], "exit4")
        self.assertTrue(exit4["fired"])


class TestTradingDaysBetween(unittest.TestCase):
    def test_same_day_is_zero(self):
        d = datetime(2026, 1, 5)  # Monday
        self.assertEqual(tb.trading_days_between(d, d), 0)

    def test_counts_weekdays_only_skips_weekend(self):
        friday = datetime(2026, 1, 2)
        monday = datetime(2026, 1, 5)
        self.assertEqual(tb.trading_days_between(friday, monday), 1)

    def test_add_trading_days_skips_weekend(self):
        friday = datetime(2026, 1, 2)
        result = tb.add_trading_days(friday, 1)
        self.assertEqual(result.strftime("%Y-%m-%d"), "2026-01-05")  # Monday, not Saturday

    def test_add_ten_trading_days(self):
        start = datetime(2026, 1, 5)  # Monday
        result = tb.add_trading_days(start, 10)
        # 10 weekdays from Monday Jan 5 -> Monday Jan 19 (two full weekends skipped)
        self.assertEqual(result.strftime("%Y-%m-%d"), "2026-01-19")


if __name__ == "__main__":
    unittest.main()
