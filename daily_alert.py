#!/usr/bin/env python3
"""Run the screener and drop the result into a Gmail draft. Meant to be triggered
by a scheduled job (see launchd.plist.example); run manually any time with:
  .venv/bin/python3 daily_alert.py
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

import gmail_draft
import screener

HERE = Path(__file__).parent
TO_EMAIL = "shahidmehmood373@gmail.com"
LOG_PATH = HERE / "daily_alert.log"


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def main() -> None:
    try:
        report = screener.build_report(min_score=60)
    except Exception:
        log("screener failed:\n" + traceback.format_exc())
        sys.exit(1)

    subject = f"Trading212 Daily Screener - {time.strftime('%Y-%m-%d')}"
    try:
        draft = gmail_draft.create_draft(TO_EMAIL, subject, report)
    except Exception:
        log("gmail draft failed:\n" + traceback.format_exc())
        sys.exit(1)

    log(f"OK - draft id={draft.get('id')}")


if __name__ == "__main__":
    main()
