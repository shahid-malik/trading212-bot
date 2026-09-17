#!/usr/bin/env python3
"""Export trades.db to a CSV for analysis in Excel/pandas/etc.

Usage:
  python3 export_trades.py trades_export.csv
"""

import csv
import sys

import trade_db


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 export_trades.py <output.csv>")
    out_path = sys.argv[1]

    conn = trade_db.get_conn()
    rows = trade_db.all_trades(conn)
    conn.close()

    if not rows:
        print("No trades logged yet.")
        return

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))

    print(f"Wrote {len(rows)} trade(s) to {out_path}")


if __name__ == "__main__":
    main()
