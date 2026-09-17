#!/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p reports
DATE=$(date +%Y-%m-%d)
.venv/bin/python3 trading_bot.py > "reports/bot_$DATE.txt" 2>&1
cp "reports/bot_$DATE.txt" reports/bot_latest.txt
