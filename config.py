"""Editable strategy parameters, externalized from trading_bot.py so the web UI
(webapp.py) can change them without touching code. Defaults here match
TRADING_RULES.md exactly; rules_config.json (gitignored is NOT set - this is
your own tuning, not a secret) overrides them once you save changes in the UI.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "rules_config.json"

DEFAULTS: dict[str, float] = {
    "max_position_per_stock": 100.0,
    "max_stock_buy_per_day": 20.0,
    "max_portfolio_buy_per_day": 50.0,
    "max_invested_pct": 80.0,
    "min_cash_pct": 20.0,
    "daily_loss_limit_pct": 1.5,
    "drawdown_reduce_pct": 5.0,
    "drawdown_reduce_factor": 0.60,
    "drawdown_stop_pct": 8.0,
    "buy_cooldown_trading_days": 3,
    "rule1_amount": 10.0,
    "rule2_amount": 10.0,
    "rule3_amount": 20.0,
    "rsi_buy_min": 55.0,
    "rsi_buy_max": 65.0,
    "emergency_stop_loss_pct": 7.0,
    "emergency_stop_cooldown_days": 10,
    "bull_profit_pct": 3.0,
    "bull_profit_sell_fraction": 0.5,
    "breakout_profit_pct": 10.0,
    "breakout_sell_fraction": 0.25,
    "breakout_volume_mult": 1.5,
    "trailing_stop_atr_mult": 2.0,
}

# Labels/groupings for the web UI form - not used by trading_bot.py itself.
FIELD_GROUPS = [
    ("Position & Daily Caps", [
        ("max_position_per_stock", "Max position per stock (EUR)"),
        ("max_stock_buy_per_day", "Max buy per stock per day (EUR)"),
        ("max_portfolio_buy_per_day", "Max portfolio buy per day (EUR)"),
        ("buy_cooldown_trading_days", "Buy cooldown (trading days)"),
    ]),
    ("Portfolio Exposure & Risk", [
        ("max_invested_pct", "Max invested (% of equity)"),
        ("min_cash_pct", "Min cash held (% of equity)"),
        ("daily_loss_limit_pct", "Daily portfolio loss limit (%)"),
        ("drawdown_reduce_pct", "Drawdown that reduces buy size (%)"),
        ("drawdown_reduce_factor", "Buy size reduction at that drawdown (fraction, e.g. 0.60 = cut 60%)"),
        ("drawdown_stop_pct", "Drawdown that stops all buying (%)"),
    ]),
    ("Buy Rules", [
        ("rule1_amount", "Buy Rule 1 (Trend) amount (EUR)"),
        ("rule2_amount", "Buy Rule 2 (RSI) amount (EUR)"),
        ("rule3_amount", "Buy Rule 3 (MACD) amount (EUR)"),
        ("rsi_buy_min", "Buy Rule 2 RSI14 minimum"),
        ("rsi_buy_max", "Buy Rule 2 RSI14 maximum"),
    ]),
    ("Exit Rules", [
        ("emergency_stop_loss_pct", "Emergency Stop loss threshold (%)"),
        ("emergency_stop_cooldown_days", "Emergency Stop re-entry cooldown (trading days)"),
        ("bull_profit_pct", "Bull Market Profit threshold (%)"),
        ("bull_profit_sell_fraction", "Bull Market Profit sell fraction (0-1)"),
        ("breakout_profit_pct", "Breakout Profit threshold (%)"),
        ("breakout_sell_fraction", "Breakout Profit sell fraction (0-1)"),
        ("breakout_volume_mult", "Breakout volume multiple (x 20d avg)"),
        ("trailing_stop_atr_mult", "Trailing stop ATR multiple"),
    ]),
]


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return {**DEFAULTS, **json.loads(CONFIG_PATH.read_text())}
    return dict(DEFAULTS)


def save_config(cfg: dict) -> None:
    # Only persist known keys, coerced to float, so the file never accumulates junk.
    clean = {k: float(cfg[k]) for k in DEFAULTS if k in cfg}
    CONFIG_PATH.write_text(json.dumps(clean, indent=2) + "\n")
