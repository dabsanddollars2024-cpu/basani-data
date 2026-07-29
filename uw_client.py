#!/usr/bin/env python3
"""
BASANI - Unusual Whales API Client (UW-only)

Writes to unusual_whales.json for the dashboard. Pulls three streams:
  1. Options flow alerts  - /api/option-trades/flow-alerts
  2. Dark pool prints     - /api/darkpool/recent
  3. Congressional trades - /api/congress/recent-trades

All endpoints go through uw_http.uw_get_json (single auth header set).

Run:  python3 uw_client.py
Cron: every 5 minutes during market hours
"""
import json
import os
import time
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(BASE_DIR))
from uw_http import uw_get_json

OUTPUT = BASE_DIR / "unusual_whales.json"

# Only flag flow alerts >= $500K as notable.
MIN_PREMIUM = 500_000

# If non-empty, restrict flow alerts to these tickers.
WATCHLIST = set()  # e.g. {"AAPL","NVDA","TSLA"} to enable


def fmt_premium(val) -> str:
    try:
        v = float(val)
    except Exception:
        return str(val) if val else ""
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:.0f}"


# ── FLOW ALERTS ───────────────────────────────────────────────────────────────
def fetch_flow() -> list:
    items = []
    data, status = uw_get_json("/api/option-trades/flow-alerts", {"limit": "200"})
    if status != 200:
        print(f"  FAIL flow: status={status}")
        return items
    rows = data if isinstance(data, list) else data.get("data", [])
    print(f"  OK flow: {len(rows)} rows")

    for row in rows:
        try:
            ticker = (row.get("ticker") or row.get("symbol") or "").upper()
            premium = (
                row.get("total_premium")
                or row.get("premium")
                or row.get("size")
                or 0
            )
            strike = row.get("strike") or row.get("strike_price") or ""
            expiry = row.get("expiry") or row.get("expiration_date") or ""
            side = (row.get("sentiment") or row.get("side") or "").lower()
            opt_type = (row.get("option_type") or row.get("call_put") or "").upper()
            ts = row.get("created_at") or row.get("timestamp") or datetime.now(timezone.utc).isoformat()
            uid = str(row.get("id") or row.get("trade_id") or ts + ticker)

            try:
                prem_val = float(str(premium).replace(",", ""))
            except Exception:
                prem_val = 0
            if prem_val < MIN_PREMIUM:
                continue
            if WATCHLIST and ticker and ticker not in WATCHLIST:
                continue

            if "bull" in side or side == "call" or opt_type == "C":
                side_norm = "bullish"
            elif "bear" in side or side == "put" or opt_type == "P":
                side_norm = "bearish"
            else:
                side_norm = side or "neutral"

            items.append({
                "id": uid,
                "timestamp": ts,
                "ticker": ticker,
                "strike": str(strike),
                "expiry": str(expiry)[:10],
                "premium": fmt_premium(prem_val),
                "side": side_norm,
                "type": "options_flow",
                "source": "unusual_whales",
                "flagged": prem_val >= 500_000,
                "channel": "flow-alerts",
                "raw": f"{ticker} {opt_type}{strike} {expiry} {fmt_premium(prem_val)} {side_norm}",
            })
        except Exception:
            continue

    return items


# ── DARK POOL ─────────────────────────────────────────────────────────────────
def fetch_darkpool() -> list:
    items = []
    data, status = uw_get_json("/api/darkpool/recent", {"limit": "200"})
    if status != 200:
        print(f"  FAIL darkpool: status={status}")
        return items
    rows = data if isinstance(data, list) else data.get("data", [])
    print(f"  OK darkpool: {len(rows)} rows")

    for row in rows:
        try:
            ticker = (row.get("ticker") or row.get("symbol") or "").upper()
            size = row.get("size") or 0
            price = row.get("price") or 0
            ts = row.get("executed_at") or row.get("timestamp") or ""
            premium = row.get("premium") or (float(size) * float(price) if size and price else 0)
            uid = str(row.get("id") or ts + ticker + str(size))
            try:
                prem_val = float(str(premium).replace(",", ""))
            except Exception:
                prem_val = 0
            if prem_val < MIN_PREMIUM:
                continue

            items.append({
                "id": uid,
                "timestamp": ts,
                "ticker": ticker,
                "size": size,
                "price": price,
                "premium": fmt_premium(prem_val),
                "type": "dark_pool",
                "source": "unusual_whales",
                "flagged": prem_val >= 500_000,
                "channel": "dark-pool",
                "raw": f"{ticker} {size}@{price} {fmt_premium(prem_val)}",
            })
        except Exception:
            continue

    return items


# ── CONGRESS ──────────────────────────────────────────────────────────────────
def fetch_congress() -> list:
    items = []
    # recent-trades returns trades by all politicians
    data, status = uw_get_json("/api/congress/recent-trades", {"limit": "100"})
    if status != 200:
        # Fallback to late-reports for filings that came in late
        data, status = uw_get_json("/api/congress/late-reports", {"limit": "100"})
        if status != 200:
            print(f"  FAIL congress: status={status}")
            return items

    rows = data if isinstance(data, list) else data.get("data", [])
    print(f"  OK congress: {len(rows)} rows")

    for row in rows:
        try:
            ticker = (row.get("ticker") or "").upper()
            tx_type = (row.get("transaction_type") or row.get("type") or "").lower()
            amount = row.get("amount") or row.get("value") or ""
            politician = row.get("name") or row.get("politician") or row.get("representative") or ""
            party = row.get("party") or ""
            ts = row.get("transaction_date") or row.get("disclosure_date") or row.get("date") or ""
            uid = str(row.get("id") or ts + ticker + politician)
            side_norm = (
                "bullish" if tx_type in ("buy", "purchase") else
                "bearish" if tx_type in ("sell", "sale") else
                tx_type or "neutral"
            )

            items.append({
                "id": uid,
                "timestamp": ts,
                "ticker": ticker,
                "politician": politician,
                "party": party,
                "side": side_norm,
                "amount": str(amount),
                "type": "congress",
                "source": "unusual_whales",
                "flagged": True,
                "channel": "congress",
                "raw": f"{politician} ({party}) {tx_type} {ticker} {amount}",
            })
        except Exception:
            continue

    return items


def main():
    t0 = time.time()
    label = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print("=" * 60)
    print(f"  BASANI Unusual Whales  --  {label}")
    print("=" * 60)

    flow = fetch_flow()
    dp = fetch_darkpool()
    cong = fetch_congress()

    combined = {"generated": label, "items": flow + dp + cong}

    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(combined, indent=2, default=str))
    os.replace(tmp, OUTPUT)

    print()
    print(f"  flow    -> {len(flow)} items")
    print(f"  dp      -> {len(dp)} items")
    print(f"  cong    -> {len(cong)} items")
    print(f"  total   -> {len(flow) + len(dp) + len(cong)} items")
    print(f"  saved   -> {OUTPUT.name} ({OUTPUT.stat().st_size//1024} KB)")
    print(f"  elapsed -> {int(time.time()-t0)}s")
    print("=" * 60)


if __name__ == "__main__":
    main()
