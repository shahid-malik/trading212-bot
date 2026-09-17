#!/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p reports
DATE=$(date +%Y-%m-%d)
.venv/bin/python3 screener.py --email "reports/$DATE.txt"
cp "reports/$DATE.txt" reports/latest.txt
