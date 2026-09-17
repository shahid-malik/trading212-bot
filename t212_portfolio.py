#!/usr/bin/env python3
"""Pull your Trading212 portfolio via the Trading212 public API.

Setup:
  1. Generate an API key in the Trading212 app: Settings > API (Beta).
     You'll be shown a key AND a secret - save both, the secret is shown once.
  2. Put them in a `.env` file next to this script:
       T212_API_KEY=your_key_here
       T212_SECRET_KEY=your_secret_here
       T212_ENV=live        # or "demo" for a practice account
     (or export T212_API_KEY / T212_SECRET_KEY / T212_ENV in your shell instead)

Usage:
  python3 t212_portfolio.py                 # print a summary table
  python3 t212_portfolio.py --json out.json # also dump raw JSON
  python3 t212_portfolio.py --csv out.csv   # also dump a CSV of positions
  python3 t212_portfolio.py --demo          # force the demo/practice API
"""

import argparse
import base64
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LIVE_BASE = "https://live.trading212.com/api/v0"
DEMO_BASE = "https://demo.trading212.com/api/v0"


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, no external dependency."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def basic_auth_header(api_key: str, api_secret: str) -> str:
    token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    return f"Basic {token}"


def api_get(base_url: str, path: str, auth_header: str, retries: int = 3):
    url = f"{base_url}{path}"
    req = urllib.request.Request(url, headers={"Authorization": auth_header})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = int(e.headers.get("Retry-After", 5))
                print(f"  Rate limited, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            body = e.read().decode(errors="replace")
            raise RuntimeError(f"{e.code} {e.reason} for {url}: {body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Failed to reach {url}: {e.reason}") from e


def fetch_portfolio(base_url: str, auth_header: str) -> list[dict]:
    return api_get(base_url, "/equity/portfolio", auth_header)


def fetch_cash(base_url: str, auth_header: str) -> dict:
    return api_get(base_url, "/equity/account/cash", auth_header)


def print_summary(positions: list[dict], cash: dict) -> None:
    if not positions:
        print("No open positions.")
    else:
        headers = ["Ticker", "Qty", "Avg Price", "Current Price", "P/L", "P/L %"]
        rows = []
        for p in positions:
            qty = p.get("quantity", 0)
            avg = p.get("averagePrice", 0)
            cur = p.get("currentPrice", 0)
            pl = p.get("ppl", 0)
            cost = qty * avg
            pl_pct = (pl / cost * 100) if cost else 0
            rows.append([
                p.get("ticker", "?"),
                f"{qty:g}",
                f"{avg:.2f}",
                f"{cur:.2f}",
                f"{pl:+.2f}",
                f"{pl_pct:+.2f}%",
            ])
        widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
        fmt = "  ".join(f"{{:<{w}}}" for w in widths)
        print(fmt.format(*headers))
        print(fmt.format(*["-" * w for w in widths]))
        for r in rows:
            print(fmt.format(*r))

    print()
    invested = cash.get("invested", 0)
    result = cash.get("result", 0)
    total = cash.get("total", 0)
    free = cash.get("free", 0)
    print(f"Invested:  {invested:,.2f}")
    print(f"P/L:       {result:+,.2f}")
    print(f"Free cash: {free:,.2f}")
    print(f"Total:     {total:,.2f}")


def write_csv(positions: list[dict], path: Path) -> None:
    fields = ["ticker", "quantity", "averagePrice", "currentPrice", "ppl", "fxPpl", "initialFillDate"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for p in positions:
            writer.writerow(p)
    print(f"Wrote {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo", action="store_true", help="Use the demo/practice API instead of live")
    parser.add_argument("--json", metavar="FILE", help="Write raw portfolio + cash JSON to FILE")
    parser.add_argument("--csv", metavar="FILE", help="Write positions to a CSV file")
    args = parser.parse_args()

    load_dotenv(Path(__file__).parent / ".env")

    api_key = os.environ.get("T212_API_KEY")
    api_secret = os.environ.get("T212_SECRET_KEY")
    if not api_key or not api_secret:
        sys.exit("Error: set T212_API_KEY and T212_SECRET_KEY (env vars or .env file). See --help.")

    auth_header = basic_auth_header(api_key, api_secret)

    env = "demo" if args.demo else os.environ.get("T212_ENV", "live").lower()
    base_url = DEMO_BASE if env == "demo" else LIVE_BASE

    print(f"Fetching portfolio from Trading212 ({env})...", file=sys.stderr)
    positions = fetch_portfolio(base_url, auth_header)
    time.sleep(1)  # be polite to the rate limiter between calls
    cash = fetch_cash(base_url, auth_header)

    print_summary(positions, cash)

    if args.json:
        Path(args.json).write_text(json.dumps({"positions": positions, "cash": cash}, indent=2))
        print(f"\nWrote {args.json}")

    if args.csv:
        write_csv(positions, Path(args.csv))


if __name__ == "__main__":
    main()
