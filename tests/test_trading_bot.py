"""Tests for the rule-evaluation logic itself: confidence-score math, exit-rule
priority ordering, and cooldown day-counting. Uses an isolated in-memory
trade_db connection per test so nothing touches the real trades.db.

IMPORTANT: trading_bot.py's thresholds (EMERGENCY_STOP_LOSS_PCT, BULL_PROFIT_PCT,
TRAILING_STOP_ATR_MULT, etc.) are read from rules_config.json at import time -
the same file the web UI's Rules page writes to. Never hardcode a specific
percentage/price in a test; always compute the test's inputs FROM tb.<CONSTANT>
so the test still exercises the intended edge case no matter what value is
currently configured. This has broken three tests so far when config values
changed via the UI mid-session - see git history.

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
    """A snapshot where every Buy Rule 1-6 condition is true by default -
    override individual fields to make specific conditions fail. Volume is set
    with headroom over tb.VOLUME_CONFIRM_MULT (config-driven, not hardcoded)
    so Rule 4 clears regardless of the currently configured multiple.
    return_10d=5.0 clears Rule 6 against evaluate_buy()'s default
    spy_return_10d=0.0 (i.e. "assume a flat market") when a test doesn't pass
    its own spy_return_10d."""
    avg_volume20 = 800_000
    base = {
        "price": 100.0, "ema20": 95.0, "sma50": 90.0, "sma200": 80.0, "sma20": 98.0,
        "rsi14": 60.0, "macd": 2.0, "macd_signal": 1.0, "macd_histogram": 1.0,
        "volume": avg_volume20 * (tb.VOLUME_CONFIRM_MULT + 0.5),
        "avg_volume20": avg_volume20, "atr14": 2.0, "prev_20d_high": 99.0,
        "return_10d": 5.0,
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
        # Kill every technical signal (trend, volume, short-term momentum,
        # relative strength) - only Rule 1's 2 always-true "not enforced"
        # conditions survive. Deliberately kills ALL rules rather than just
        # trend, so this doesn't need updating every time a new rule is added
        # (that's broken this test twice already - see git history).
        snap = full_snap(ema20=200.0, sma50=200.0, sma200=200.0,  # kills trend (rules 1/2/3/5)
                          volume=1.0, avg_volume20=1_000_000,      # kills rule 4
                          return_10d=-5.0)                          # kills rule 6 (underperforms flat market)
        decisions = tb.evaluate_buy("TEST", None, snap, True, self.pstate, self.conn, "2026-01-01")
        confidence = decisions[-1]
        self.assertLess(confidence["confidence_pct"], 50.0)
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

    def test_decisions_list_has_six_rule_components_plus_confidence(self):
        decisions = tb.evaluate_buy("TEST", None, full_snap(), True, self.pstate, self.conn, "2026-01-01")
        rules = [d["rule"] for d in decisions]
        self.assertEqual(rules, ["gates", "rule1", "rule2", "rule3", "rule4", "rule5", "rule6", "confidence"])

    def test_rule4_volume_confirmation_fires_above_multiple(self):
        avg_vol = 1_000_000
        snap = full_snap(volume=avg_vol * (tb.VOLUME_CONFIRM_MULT + 0.1), avg_volume20=avg_vol)
        decisions = tb.evaluate_buy("TEST", None, snap, True, self.pstate, self.conn, "2026-01-01")
        rule4 = next(d for d in decisions if d["rule"] == "rule4")
        self.assertTrue(rule4["fired"])

    def test_rule4_volume_confirmation_fails_below_multiple(self):
        avg_vol = 1_000_000
        snap = full_snap(volume=avg_vol * (tb.VOLUME_CONFIRM_MULT - 0.1), avg_volume20=avg_vol)
        decisions = tb.evaluate_buy("TEST", None, snap, True, self.pstate, self.conn, "2026-01-01")
        rule4 = next(d for d in decisions if d["rule"] == "rule4")
        self.assertFalse(rule4["fired"])

    def test_rule5_short_term_momentum_requires_both_conditions(self):
        # Price above SMA20 but SMA20 below SMA50 - a short-term dip inside a
        # longer uptrend, not confirmed short-term momentum yet.
        snap = full_snap(sma20=85.0, sma50=90.0, price=87.0)
        decisions = tb.evaluate_buy("TEST", None, snap, True, self.pstate, self.conn, "2026-01-01")
        rule5 = next(d for d in decisions if d["rule"] == "rule5")
        self.assertFalse(rule5["fired"])
        self.assertIn(("Rule 5 - Price > SMA20", True), rule5["conditions"])
        self.assertIn(("Rule 5 - SMA20 > SMA50", False), rule5["conditions"])

    def test_rule5_short_term_momentum_fires_when_aligned(self):
        snap = full_snap(sma20=98.0, sma50=90.0, price=100.0)  # price > sma20 > sma50
        decisions = tb.evaluate_buy("TEST", None, snap, True, self.pstate, self.conn, "2026-01-01")
        rule5 = next(d for d in decisions if d["rule"] == "rule5")
        self.assertTrue(rule5["fired"])

    def test_new_rules_included_in_confidence_weighting(self):
        # Killing only Rule 4+5 (both technical, not shared with 1/2/3) must
        # reduce confidence below 100% even though Rules 1/2/3 are untouched.
        snap = full_snap(volume=1.0, avg_volume20=1_000_000, sma20=200.0)  # kills rule4 and rule5
        decisions = tb.evaluate_buy("TEST", None, snap, True, self.pstate, self.conn, "2026-01-01")
        confidence = decisions[-1]
        self.assertLess(confidence["confidence_pct"], 100.0)

    def test_rule6_relative_strength_fires_when_outperforming_spy(self):
        snap = full_snap(return_10d=8.0)
        decisions = tb.evaluate_buy("TEST", None, snap, True, self.pstate, self.conn, "2026-01-01", spy_return_10d=3.0)
        rule6 = next(d for d in decisions if d["rule"] == "rule6")
        self.assertTrue(rule6["fired"])

    def test_rule6_relative_strength_fails_when_underperforming_spy(self):
        snap = full_snap(return_10d=2.0)
        decisions = tb.evaluate_buy("TEST", None, snap, True, self.pstate, self.conn, "2026-01-01", spy_return_10d=5.0)
        rule6 = next(d for d in decisions if d["rule"] == "rule6")
        self.assertFalse(rule6["fired"])

    def test_rule6_defaults_to_flat_market_when_spy_return_not_supplied(self):
        # No spy_return_10d passed - defaults to 0.0 ("assume flat market").
        # A positive ticker return should still count as outperformance.
        snap = full_snap(return_10d=1.0)
        decisions = tb.evaluate_buy("TEST", None, snap, True, self.pstate, self.conn, "2026-01-01")
        rule6 = next(d for d in decisions if d["rule"] == "rule6")
        self.assertTrue(rule6["fired"])

    def test_rule6_fails_gracefully_when_return_10d_missing(self):
        snap = full_snap(return_10d=None)
        decisions = tb.evaluate_buy("TEST", None, snap, True, self.pstate, self.conn, "2026-01-01")
        rule6 = next(d for d in decisions if d["rule"] == "rule6")
        self.assertFalse(rule6["fired"])

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
        # Loss 1pt past whatever the configured threshold currently is (these
        # constants are read from rules_config.json at import time, so this
        # must not hardcode a specific percentage - the threshold is meant to
        # be tunable via the web UI's Rules page).
        loss_pct = tb.EMERGENCY_STOP_LOSS_PCT + 1
        current_price = 100.0 * (1 - loss_pct / 100)
        position = {"quantity": 1.0, "averagePrice": 100.0, "currentPrice": current_price, "ppl": -loss_pct}
        snap = full_snap(price=current_price)
        decisions = tb.evaluate_exit(position, snap, self._pstate(), True)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["rule"], "exit1")
        self.assertTrue(decisions[0]["fired"])
        self.assertEqual(decisions[0]["sell_fraction"], 1.0)

    def test_small_loss_does_not_trigger_emergency_stop(self):
        # Half the configured threshold - safely under it regardless of value.
        loss_pct = tb.EMERGENCY_STOP_LOSS_PCT / 2
        current_price = 100.0 * (1 - loss_pct / 100)
        position = {"quantity": 1.0, "averagePrice": 100.0, "currentPrice": current_price, "ppl": -loss_pct}
        decisions = tb.evaluate_exit(position, full_snap(price=current_price), self._pstate(), True)
        exit1 = decisions[0]
        self.assertEqual(exit1["rule"], "exit1")
        self.assertFalse(exit1["fired"])

    def test_bull_profit_fires_once_then_locked(self):
        profit_pct = tb.BULL_PROFIT_PCT + 1
        current_price = 100.0 * (1 + profit_pct / 100)
        position = {"quantity": 1.0, "averagePrice": 100.0, "currentPrice": current_price, "ppl": profit_pct}
        snap = full_snap(price=current_price)
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
        # Construct highest_price so trailing_level = current_price + 5,
        # guaranteeing a trigger regardless of the configured ATR multiple.
        # profit_pct is kept tiny so bull-profit/breakout thresholds (also
        # config-driven, could be as low as a few percent) can't fire first
        # and change which rule ends up last in the returned list.
        current_price = 100.5
        atr14 = 1.0
        highest = current_price + tb.TRAILING_STOP_ATR_MULT * atr14 + 5
        position = {"quantity": 1.0, "averagePrice": 100.0, "currentPrice": current_price, "ppl": 0.5}
        snap = full_snap(price=current_price, atr14=atr14)
        armed = self._pstate(trailing_stop_active=1, trailing_stop_highest_price=highest)
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


class TestLoggedTimestampMatchesRunId(unittest.TestCase):
    """Regression test for a real bug: log_buy/log_sell/log_decision used to
    stamp trades with time.strftime() (real wall-clock time) instead of the
    caller-supplied run_id. That's harmless for the live bot (run_id IS real
    time there) but silently broke backtest.py: every simulated historical
    trade got stamped as "today", which made every subsequent
    last_buy_timestamp()-based cooldown check compare a future timestamp
    against an earlier simulated date, permanently locking each ticker out
    after its first-ever buy. A 5-year backtest went from 13 buys to 165 once
    this was fixed - see git history for the full writeup."""

    def setUp(self):
        self.conn = make_conn()

    def test_log_buy_uses_run_id_not_wall_clock(self):
        historical_run_id = "2022-03-15"  # deliberately not today's real date
        confidence = {"rule": "confidence", "label": "Confidence Buy Rule",
                      "rule1_fired": True, "rule2_fired": True, "rule3_fired": True, "rule4_fired": True, "rule5_fired": True, "rule6_fired": True,
                      "confidence_pct": 95.0}
        snap = full_snap()
        trade_id = tb.log_buy(self.conn, "TEST", "TEST", None, snap, confidence, 20.0, True, historical_run_id)
        row = self.conn.execute("SELECT timestamp FROM trades WHERE id = ?", (trade_id,)).fetchone()
        self.assertEqual(row[0], historical_run_id)

    def test_log_sell_uses_run_id_not_wall_clock(self):
        historical_run_id = "2022-03-15"
        position = {"quantity": 1.0, "averagePrice": 100.0, "currentPrice": 90.0, "ppl": -10.0}
        d = {"rule": "exit1", "label": "Exit Rule 1 - Emergency Stop", "profit_pct": -10.0}
        trade_id = tb.log_sell(self.conn, "TEST", "TEST", position, full_snap(price=90.0), d, 1.0, 90.0, True, historical_run_id)
        row = self.conn.execute("SELECT timestamp FROM trades WHERE id = ?", (trade_id,)).fetchone()
        self.assertEqual(row[0], historical_run_id)

    def test_cooldown_correctly_expires_against_a_later_simulated_date(self):
        # The actual failure mode: a buy dated (correctly) in the past, then a
        # cooldown check against a LATER simulated date, must find the
        # cooldown expired once enough trading days have passed.
        confidence = {"rule": "confidence", "label": "Confidence Buy Rule",
                      "rule1_fired": True, "rule2_fired": True, "rule3_fired": True, "rule4_fired": True, "rule5_fired": True, "rule6_fired": True,
                      "confidence_pct": 95.0}
        tb.log_buy(self.conn, "TEST", "TEST", None, full_snap(), confidence, 20.0, True, "2022-01-03")
        pstate = trade_db.get_position_state(self.conn, "TEST")
        decisions = tb.evaluate_buy("TEST", None, full_snap(), True, pstate, self.conn, "2022-01-10")
        gates = next(d for d in decisions if d["rule"] == "gates")
        self.assertTrue(gates["fired"], f"cooldown should have expired by 2022-01-10: {gates['blocks']}")


if __name__ == "__main__":
    unittest.main()
