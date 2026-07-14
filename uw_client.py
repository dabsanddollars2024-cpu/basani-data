#!/usr/bin/env python3
"""
BASANI — Unusual Whales API Client
Pulls live options flow, dark pool prints, and congressional trades.
Writes to unusual_whales.json for the dashboard.

Run manually: python3 uw_client.py
Auto-run:     added to cron via setup_cron.sh
"""

import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ── CONFIG ────────────────────────────────────────────────────────────────────
UW_TOKEN   = os.environ.get("UW_TOKEN", "")
BASE_URL   = "https://api.unusualwhales.com"
DIR        = os.path.dirname(os.path.abspath(__file__))
OUTPUT     = os.path.join(DIR, "unusual_whales.json")
MAX_ALERTS = 200

# Leave WATCHLIST empty to capture ALL tickers (recommended for Basic plan)
# Add specific tickers only if you want to filter down
WATCHLIST = []  # empty = accept everything

# Min premium to capture — keep low so Basic plan data comes through
MIN_PREMIUM = 10_000  # $10k+ (Basic plan has lower volume than Advanced)

# ── HTTP HELPER ───────────────────────────────────────────────────────────────
def get(path, params=None):
    url = BASE_URL + path
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url += "?" + query
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + UW_TOKEN,
        "Accept":        "application/json",
        "User-Agent":    "BASANI/1.0"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        return (json.loads(raw) if raw else {}), e.code
    except Exception as e:
        return {"error": str(e)}, 0

# ── DATA HELPERS ──────────────────────────────────────────────────────────────
def load_existing():
    if os.path.exists(OUTPUT):
        try:
            with open(OUTPUT) as f:
                data = json.load(f)
            # Handle both list format and dict format {"items": [...]}
            if isinstance(data, dict):
                return data.get("items", [])
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []

def save(alerts):
    # Deduplicate by id, sort newest first, cap at MAX
    seen = set()
    unique = []
    for a in sorted(alerts, key=lambda x: x.get("timestamp",""), reverse=True):
        aid = a.get("id") or a.get("timestamp","") + a.get("ticker","")
        if aid not in seen:
            seen.add(aid)
            unique.append(a)
    final = unique[:MAX_ALERTS]
    with open(OUTPUT, "w") as f:
        json.dump({
            "generated": datetime.now(timezone.utc).isoformat(),
            "count": len(final),
            "items": final
        }, f, indent=2)
    return len(final)

def fmt_premium(val):
    try:
        v = float(val)
        if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
        if v >= 1_000:     return f"${v/1_000:.0f}K"
        return f"${v:.0f}"
    except Exception:
        return str(val) if val else ""

# ── FLOW ALERTS ───────────────────────────────────────────────────────────────
def fetch_flow():
    """Pull latest options flow alerts — tries multiple endpoints."""
    items = []
    # Try endpoints in priority order (Basic plan may only have some of these)
    endpoints = [
        ("/api/option-trades/flow-alerts", {"limit": "200"}),
        ("/api/flow/alerts",               {"limit": "200"}),
        ("/api/option-trades/latest",      {"limit": "200"}),
    ]
    data, status = {}, 0
    for path, params in endpoints:
        data, status = get(path, params)
        if status == 200:
            print(f"  flow endpoint: {path}")
            break
        print(f"  SKIP flow {path} ({status})")

    if status != 200:
        print(f"  FAIL all flow endpoints — check API plan at unusualwhales.com")
        return items

    rows = data if isinstance(data, list) else data.get("data", [])
    now  = datetime.now(timezone.utc)

    for row in rows:
        try:
            ticker   = (row.get("ticker") or row.get("symbol") or "").upper()
            premium  = row.get("total_premium") or row.get("premium") or row.get("size") or 0
            strike   = row.get("strike") or row.get("strike_price") or ""
            expiry   = row.get("expiry") or row.get("expiration_date") or ""
            side     = (row.get("sentiment") or row.get("side") or row.get("call_put") or "").lower()
            opt_type = (row.get("option_type") or row.get("call_put") or "").upper()
            ts       = row.get("created_at") or row.get("timestamp") or now.isoformat()
            uid      = str(row.get("id") or row.get("trade_id") or ts + ticker)

            # Filter
            try:
                prem_val = float(str(premium).replace(",",""))
            except Exception:
                prem_val = 0
            if prem_val < MIN_PREMIUM:
                continue
            # Only filter by watchlist if it's not empty
            if WATCHLIST and ticker and ticker not in WATCHLIST:
                continue

            # Normalize side
            if "bull" in side or side == "call" or opt_type == "C":
                side_norm = "bullish"
            elif "bear" in side or side == "put" or opt_type == "P":
                side_norm = "bearish"
            else:
                side_norm = side or "neutral"

            flagged = prem_val >= 500_000  # Flag $500k+ as unusual

            items.append({
                "id":        uid,
                "timestamp": ts,
                "ticker":    ticker,
                "strike":    str(strike),
                "expiry":    str(expiry)[:10],
                "premium":   fmt_premium(prem_val),
                "side":      side_norm,
                "type":      "options_flow",
                "source":    "unusual_whales",
                "flagged":   flagged,
                "channel":   "flow-alerts",
                "raw":       f"{ticker} {opt_type}{strike} {expiry} {fmt_premium(prem_val)} {side_norm}"
            })
        except Exception as e:
            continue

    return items

# ── DARK POOL ─────────────────────────────────────────────────────────────────
def fetch_darkpool():
    """Pull recent dark pool prints."""
    items = []
    data, status = get("/api/darkpool/recent", {"limit": "50"})

    if status != 200:
        print(f"  FAIL darkpool ({status}): {data}")
        return items

    rows = data if isinstance(data, list) else data.get("data", [])
    now  = datetime.now(timezone.utc)

    for row in rows:
        try:
            ticker  = (row.get("ticker") or row.get("symbol") or "").upper()
            premium = row.get("premium") or row.get("size") or row.get("notional") or 0
            ts      = row.get("executed_at") or row.get("timestamp") or now.isoformat()
            uid     = str(row.get("id") or ts + ticker + "dp")

            try:
                prem_val = float(str(premium).replace(",",""))
            except Exception:
                prem_val = 0
            if prem_val < MIN_PREMIUM:
                continue
            if WATCHLIST and ticker and ticker not in WATCHLIST:
                continue

            items.append({
                "id":        uid,
                "timestamp": ts,
                "ticker":    ticker,
                "strike":    "",
                "expiry":    "",
                "premium":   fmt_premium(prem_val),
                "side":      "dark pool",
                "type":      "dark_pool",
                "source":    "unusual_whales",
                "flagged":   prem_val >= 1_000_000,
                "channel":   "dark-pool",
                "raw":       f"🌑 DARK POOL {ticker} {fmt_premium(prem_val)}"
            })
        except Exception:
            continue

    return items

# ── CONGRESSIONAL TRADES ──────────────────────────────────────────────────────
def fetch_congress():
    """Pull recent congressional trades — free signal."""
    items = []
    data, status = get("/api/congressional-trades", {"limit": "20"})

    if status != 200:
        print(f"  SKIP congress ({status})")
        return items

    rows = data if isinstance(data, list) else data.get("data", [])

    for row in rows:
        try:
            ticker    = (row.get("ticker") or "").upper()
            name      = row.get("representative") or row.get("senator") or "Congress"
            trade_type = (row.get("transaction_type") or row.get("type") or "").lower()
            amount    = row.get("amount") or row.get("size") or ""
            ts        = row.get("disclosure_date") or row.get("transaction_date") or ""
            uid       = str(row.get("id") or ts + ticker + "cong")

            if not ticker:
                continue

            side = "bullish" if "purchase" in trade_type or "buy" in trade_type else "bearish" if "sale" in trade_type or "sell" in trade_type else "neutral"

            items.append({
                "id":        uid,
                "timestamp": ts + "T12:00:00Z" if ts and "T" not in ts else ts,
                "ticker":    ticker,
                "strike":    "",
                "expiry":    "",
                "premium":   str(amount),
                "side":      side,
                "type":      "congressional",
                "source":    "unusual_whales",
                "flagged":   True,
                "channel":   "congress",
                "raw":       f"🏛️ {name} {trade_type.upper()} {ticker} {amount}"
            })
        except Exception:
            continue

    return items

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("")
    print("=" * 52)
    print("  BASANI Unusual Whales  --  " + datetime.now().strftime("%Y-%m-%d %H:%M"))
    print("=" * 52)

    existing = load_existing()
    new_items = []

    # Flow alerts
    flow = fetch_flow()
    new_items.extend(flow)
    print(f"  flow alerts   → {len(flow)} captured")

    # Dark pool
    dp = fetch_darkpool()
    new_items.extend(dp)
    print(f"  dark pool     → {len(dp)} captured")

    # Congressional
    cong = fetch_congress()
    new_items.extend(cong)
    print(f"  congressional → {len(cong)} captured")

    # Merge with existing and save
    all_alerts = existing + new_items
    total = save(all_alerts)
    print(f"  total saved   → {total} alerts in unusual_whales.json")
    print("=" * 52)
    print("")
