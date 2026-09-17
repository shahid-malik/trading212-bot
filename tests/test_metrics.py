"""Tests for the KPI calculations that back the Dashboard page. Since these
numbers are what a user reads to judge "is this strategy any good," they need
to be right on known inputs, not just "doesn't crash."

Run: .venv/bin/python3 -m unittest discover -s tests -t .
"""

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import metrics
import trade_db


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(trade_db.SCHEMA)
    return conn


def seed_equity(conn, points):
    for date, equity in points:
        trade_db.record_equity_snapshot(conn, date, cash=0, invested_value=equity, total_equity=equity)


class TestEquityCurveMetrics(unittest.TestCase):
    def test_total_return_pct(self):
        curve = [("2026-01-01", 1000.0), ("2026-06-01", 1100.0)]
        self.assertAlmostEqual(metrics.total_return_pct(curve), 10.0)

    def test_total_return_pct_loss(self):
        curve = [("2026-01-01", 1000.0), ("2026-06-01", 800.0)]
        self.assertAlmostEqual(metrics.total_return_pct(curve), -20.0)

    def test_total_return_none_for_short_curve(self):
        self.assertIsNone(metrics.total_return_pct([("2026-01-01", 1000.0)]))

    def test_max_drawdown_simple_dip(self):
        curve = [("d1", 1000.0), ("d2", 1200.0), ("d3", 900.0), ("d4", 1100.0)]
        # peak 1200 -> trough 900 = 25% drawdown
        self.assertAlmostEqual(metrics.max_drawdown_pct(curve), 25.0)

    def test_max_drawdown_monotonic_increase_is_zero(self):
        curve = [("d1", 1000.0), ("d2", 1100.0), ("d3", 1200.0)]
        self.assertAlmostEqual(metrics.max_drawdown_pct(curve), 0.0)

    def test_max_drawdown_never_recovers(self):
        curve = [("d1", 1000.0), ("d2", 500.0)]
        self.assertAlmostEqual(metrics.max_drawdown_pct(curve), 50.0)

    def test_cagr_one_year_doubling(self):
        curve = [("2025-01-01", 1000.0), ("2026-01-01", 2000.0)]
        self.assertAlmostEqual(metrics.cagr_pct(curve), 100.0, delta=1.0)

    def test_cagr_none_for_flat_dates(self):
        curve = [("2026-01-01", 1000.0), ("2026-01-01", 1100.0)]
        self.assertIsNone(metrics.cagr_pct(curve))

    def test_sharpe_zero_variance_is_none(self):
        # Constant daily return -> zero variance -> undefined Sharpe (division by zero avoided).
        curve = [("d1", 1000.0), ("d2", 1010.0), ("d3", 1020.1)]
        # not exactly constant % but close; test the real zero-variance case instead:
        flat_curve = [("d1", 1000.0), ("d2", 1000.0), ("d3", 1000.0)]
        self.assertIsNone(metrics.sharpe_ratio(flat_curve))

    def test_sharpe_positive_for_steady_gains(self):
        curve = [(f"d{i}", 1000.0 * (1.001 ** i)) for i in range(30)]
        sharpe = metrics.sharpe_ratio(curve)
        self.assertIsNotNone(sharpe)
        self.assertGreater(sharpe, 0)


class TestTradeStats(unittest.TestCase):
    def _log_sell(self, conn, ticker, profit_loss_pct, order_size_eur=100.0):
        trade_db.record_trade(
            conn, timestamp="2026-01-01T10:00:00", ticker=ticker, yahoo_symbol=ticker,
            action="SELL", order_size_eur=order_size_eur, quantity=1.0, price=100.0,
            avg_price=100.0, current_price=100.0, profit_loss_pct=profit_loss_pct,
            profit_loss_eur=0.0, reason="test", order_result="DRY_RUN", dry_run=1,
        )

    def test_no_trades_returns_none_stats(self):
        conn = make_conn()
        stats = metrics.trade_stats(conn)
        self.assertEqual(stats["sell_count"], 0)
        self.assertIsNone(stats["win_rate_pct"])

    def test_all_wins_100_percent_win_rate(self):
        conn = make_conn()
        self._log_sell(conn, "A", 5.0)
        self._log_sell(conn, "B", 10.0)
        stats = metrics.trade_stats(conn)
        self.assertAlmostEqual(stats["win_rate_pct"], 100.0)
        self.assertAlmostEqual(stats["avg_win_pct"], 7.5)
        self.assertEqual(stats["avg_loss_pct"], 0.0)

    def test_mixed_wins_and_losses(self):
        conn = make_conn()
        self._log_sell(conn, "A", 10.0)   # win
        self._log_sell(conn, "B", -5.0)   # loss
        self._log_sell(conn, "C", 20.0)   # win
        self._log_sell(conn, "D", -10.0)  # loss
        stats = metrics.trade_stats(conn)
        self.assertAlmostEqual(stats["win_rate_pct"], 50.0)
        self.assertAlmostEqual(stats["avg_win_pct"], 15.0)
        self.assertAlmostEqual(stats["avg_loss_pct"], -7.5)

    def test_zero_pct_is_a_loss_not_a_win(self):
        conn = make_conn()
        self._log_sell(conn, "A", 0.0)
        stats = metrics.trade_stats(conn)
        self.assertAlmostEqual(stats["win_rate_pct"], 0.0)

    def test_profit_factor_all_losses_is_zero(self):
        # gross_profit=0 with gross_loss>0 -> 0.0, not None. None is reserved
        # for "no sell data at all" (see test_no_trades_returns_none_stats).
        conn = make_conn()
        self._log_sell(conn, "A", -5.0)
        stats = metrics.trade_stats(conn)
        self.assertEqual(stats["profit_factor"], 0.0)

    def test_profit_factor_all_wins_is_infinite(self):
        conn = make_conn()
        self._log_sell(conn, "A", 5.0)
        stats = metrics.trade_stats(conn)
        self.assertEqual(stats["profit_factor"], float("inf"))


class TestRuleAccuracy(unittest.TestCase):
    def _log_buy(self, conn, ticker, timestamp, rule1=1, rule2=0, rule3=0):
        trade_db.record_trade(
            conn, timestamp=timestamp, ticker=ticker, yahoo_symbol=ticker,
            action="BUY", order_size_eur=20.0, quantity=1.0, price=100.0,
            avg_price=None, current_price=100.0, profit_loss_pct=None, profit_loss_eur=None,
            reason="test", order_result="DRY_RUN", dry_run=1,
            rule1_fired=rule1, rule2_fired=rule2, rule3_fired=rule3, confidence_pct=95.0,
        )

    def _log_sell(self, conn, ticker, timestamp, profit_loss_pct):
        trade_db.record_trade(
            conn, timestamp=timestamp, ticker=ticker, yahoo_symbol=ticker,
            action="SELL", order_size_eur=20.0, quantity=1.0, price=100.0,
            avg_price=100.0, current_price=100.0, profit_loss_pct=profit_loss_pct,
            profit_loss_eur=0.0, reason="test", order_result="DRY_RUN", dry_run=1,
        )

    def test_rule1_signal_traced_to_next_sell_outcome(self):
        conn = make_conn()
        self._log_buy(conn, "A", "2026-01-01T10:00:00", rule1=1)
        self._log_sell(conn, "A", "2026-01-05T10:00:00", profit_loss_pct=8.0)  # win
        results = metrics.rule_accuracy(conn)
        rule1 = next(r for r in results if r["rule"] == "Rule 1 - Trend")
        self.assertEqual(rule1["buys_with_signal"], 1)
        self.assertAlmostEqual(rule1["next_sell_profitable_pct"], 100.0)

    def test_only_counts_buys_where_that_specific_rule_fired(self):
        conn = make_conn()
        self._log_buy(conn, "A", "2026-01-01T10:00:00", rule1=1, rule2=0, rule3=0)
        self._log_sell(conn, "A", "2026-01-05T10:00:00", profit_loss_pct=8.0)
        results = metrics.rule_accuracy(conn)
        rule2 = next(r for r in results if r["rule"] == "Rule 2 - RSI")
        self.assertEqual(rule2["buys_with_signal"], 0)
        self.assertIsNone(rule2["next_sell_profitable_pct"])

    def test_no_subsequent_sell_yet_excluded_from_stats(self):
        conn = make_conn()
        self._log_buy(conn, "A", "2026-01-01T10:00:00", rule1=1)  # still open, no sell logged
        results = metrics.rule_accuracy(conn)
        rule1 = next(r for r in results if r["rule"] == "Rule 1 - Trend")
        self.assertEqual(rule1["buys_with_signal"], 0)


if __name__ == "__main__":
    unittest.main()
