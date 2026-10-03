#!/bin/bash
cd "$(dirname "$0")"
mkdir -p reports
DATE=$(date +%Y-%m-%d)
.venv/bin/python3 trading_bot.py > "reports/bot_$DATE.txt" 2>&1
BOT_STATUS=$?
cp "reports/bot_$DATE.txt" reports/bot_latest.txt
# Daily buy/sell email (SMTP settings in .env). Sent even if the bot failed, so a
# broken run shows up in your inbox instead of passing silently.
.venv/bin/python3 daily_trade_email.py --report "reports/bot_$DATE.txt"
exit $BOT_STATUS
