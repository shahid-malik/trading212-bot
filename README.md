# Defensive Trading Bot for Trading 212

An algorithmic trading bot for Trading 212 focused on **defensive, rule-based trend and momentum trading**. The system combines market-regime analysis, technical indicators, position sizing, portfolio risk management, profit protection, and automated entry/exit rules.

### Core Strategy

The bot evaluates:

* **SPY market regime** using SMA50 and SMA200
* **Stock trend** using EMA20, SMA50, and SMA200
* **Momentum** using RSI(14) and MACD
* **Trading activity** using volume and relative volume
* **Volatility** using ATR(14)
* **Liquidity and bid/ask spread**
* **Earnings-event protection**
* **Portfolio exposure and drawdown**

### Risk Management

The strategy is designed for capital preservation:

* Maximum 70% portfolio exposure
* Minimum 30% cash reserve
* Maximum €100 exposure per stock
* Small incremental entries
* No averaging down
* Maximum portfolio risk of 2%
* Daily loss protection
* 5% drawdown reduces new purchases
* 8% drawdown stops new purchases
* Sector/correlation exposure limits
* Stop-loss and trailing-stop protection
* Position cooldowns after losses

### Trading Logic

The bot does not trade based on a single indicator. It combines multiple confirmations into an **entry score** and only considers trades when the broader market and individual stock trend are favorable.

The strategy supports:

* Trend-following entries
* Momentum entries
* Pullback entries
* Breakout entries
* Incremental position building
* Profit taking
* Trailing stops
* Trend-based exits
* Market-regime protection

### Development Modes

The project should support three completely separate execution modes:

1. **Backtest** — evaluate the strategy against historical data
2. **Paper Trading** — execute simulated trades using real-time market conditions
3. **Live Trading** — execute real orders through the Trading 212 API

Live trading must remain disabled by default.

### Safety First

Every order must pass the risk engine before execution. Risk-management rules always override trading signals.

The default action when conditions are unclear is:

**DO NOTHING.**

This project is intended as an experimental algorithmic trading system and does **not guarantee profits or financial returns**. All strategies should be thoroughly backtested, stress-tested, and paper-traded before any real capital is used.
