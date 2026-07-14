#!/usr/bin/env python3
"""
BASANI — Massive (formerly Polygon) Data Feed
==============================================
Role: Raw market data input layer ONLY.
      Fetches prices, candles, volume, and options chain data.
      Does NOT generate signals, flow analysis, or trade ideas.

Saves:  massive_data.json  (raw, normalized market data)

Data provided:
  - Stock snapshots (price, volume, OHLC)
  - Historical daily candles (90 days)
  - Options chains (strikes, expirations, IV, open interest)

Data NOT provided by this module:
  - Options flow / unusual activity  → use uw_client.py
  - News / sentiment                 → use news_feed.py
  - Trade signals                    → use scan.py

If Massive data is unavailable, this module saves an explicit error
state and does NOT substitute with unrelated data.
"""

import json
import os
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import date, timedelta, datetime

# ── Configuration ──────────────────────────────────────────────────────────────

BASE_URL   = "https://api.polygon.io"   # Massive API endpoint (formerly Polygon)
API_KEY    = os.environ.get("MASSIVE_KEY", "")

DIR        = os.path.dirname(os.path.abspath(__file__))
OUTPUT     = os.path.join(DIR, "massive_data.json")

# Tickers to fetch snapshots + candles for
TICKERS = [
    "SPY", "QQQ", "AAPL", "NVDA", "AMD", "MSFT", "AMZN", "GOOGL", "META", "TSLA",
    "MU", "PLTR", "COIN", "ARM", "CRM", "NOW", "SMCI",
    "NFLX", "UBER", "SHOP", "MSTR",
    "XLF", "XLE", "XLK", "GLD", "TLT", "SOXS"
]

# Tickers to fetch full options chains for (expensive — keep focused)
OPTIONS_TICKERS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "AMD", "META"]

# ── HTTP Helper ────────────────────────────────────────────────────────────────

def api_get(path, params=None):
    """
    Make an authenticated GET request to the Massive/Polygon API.
    Returns (data_dict, error_string). If successful, error is None.
    If failed, data is None and error describes what went wrong.
    """
    if not API_KEY:
        return None, "MASSIVE_KEY not set — add it to environment variables"

    p = params or {}
    p["apiKey"] = API_KEY
    url = BASE_URL + path + "?" + urllib.parse.urlencode(p)

    req = urllib.request.Request(url, headers={"User-Agent": "BASANI/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
            return data, None
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            msg = json.loads(body).get("error", body[:200])
        except Exception:
            msg = body[:200]
        return None, f"HTTP {e.code}: {msg}"
    except urllib.error.URLError as e:
        return None, f"Network error: {e.reason}"
    except Exception as e:
        return None, f"Unexpected error: {e}"


# ── Fetch Functions ────────────────────────────────────────────────────────────

def fetch_snapshots(tickers):
    """
    Fetch current price snapshot for a list of tickers.
    Returns list of normalized snapshot dicts, or empty list on error.
    """
    print("  Fetching stock snapshots...")
    data, err = api_get(
        "/v2/snapshot/locale/us/markets/stocks/tickers",
        {"tickers": ",".join(tickers)}
    )
    if err:
        print(f"  ❌ Snapshots error: {err}")
        return [], err

    snapshots = []
    for item in (data or {}).get("tickers", []):
        day   = item.get("day", {})
        prev  = item.get("prevDay", {})
        trade = item.get("lastTrade", {})
        quote = item.get("lastQuote", {})
        ticker = item.get("ticker", "")

        price    = trade.get("p") or day.get("c") or 0
        prev_cls = prev.get("c") or price
        chg_pct  = round((price - prev_cls) / prev_cls * 100, 2) if prev_cls else 0

        snapshots.append({
            "ticker":    ticker,
            "price":     round(price, 4),
            "prev_close": round(prev_cls, 4),
            "chg_pct":   chg_pct,
            "open":      day.get("o"),
            "high":      day.get("h"),
            "low":       day.get("l"),
            "close":     day.get("c"),
            "volume":    day.get("v"),
            "vwap":      day.get("vw"),
            "prev_volume": prev.get("v"),
            "bid":       quote.get("P"),
            "ask":       quote.get("P"),
            "source":    "massive"
        })

    print(f"  ✅ Got {len(snapshots)} snapshots")
    return snapshots, None


def fetch_candles(ticker, days=90):
    """
    Fetch daily OHLCV candles for one ticker over the past N days.
    Returns list of candle dicts, or empty list on error.
    """
    from_date = (date.today() - timedelta(days=days)).isoformat()
    to_date   = date.today().isoformat()

    data, err = api_get(
        f"/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}",
        {"adjusted": "true", "sort": "asc", "limit": 365}
    )
    if err:
        return [], err

    results = data.get("results", []) if data else []
    candles = []
    for r in results:
        candles.append({
            "date":   datetime.utcfromtimestamp(r["t"] / 1000).strftime("%Y-%m-%d"),
            "open":   r.get("o"),
            "high":   r.get("h"),
            "low":    r.get("l"),
            "close":  r.get("c"),
            "volume": r.get("v"),
            "vwap":   r.get("vw"),
            "source": "massive"
        })
    return candles, None


def fetch_options_chain(ticker):
    """
    Fetch options chain for one ticker.
    Returns normalized list of option contracts, or empty list on error.

    Fields per contract:
      strike_price, expiration_date, contract_type (call/put),
      implied_volatility, open_interest, volume, delta, gamma, theta
    """
    data, err = api_get(
        f"/v3/snapshot/options/{ticker}",
        {"limit": 250}
    )
    if err:
        return [], err

    contracts = []
    for item in (data or {}).get("results", []):
        details = item.get("details", {})
        greeks  = item.get("greeks", {})
        day     = item.get("day", {})

        contracts.append({
            "ticker":            ticker,
            "strike":            details.get("strike_price"),
            "expiration":        details.get("expiration_date"),
            "type":              details.get("contract_type"),   # "call" or "put"
            "implied_volatility": item.get("implied_volatility"),
            "open_interest":     item.get("open_interest"),
            "volume":            day.get("volume"),
            "last_price":        day.get("close"),
            "delta":             greeks.get("delta"),
            "gamma":             greeks.get("gamma"),
            "theta":             greeks.get("theta"),
            "vega":              greeks.get("vega"),
            "source":            "massive"
        })

    return contracts, None


# ── Main ───────────────────────────────────────────────────────────────────────

def run():
    now = datetime.now()
    ts  = now.strftime("%Y-%m-%d %H:%M:%S")

    print("")
    print("=" * 60)
    print(f"  BASANI Massive Feed  —  {ts}")
    print("=" * 60)

    if not API_KEY:
        print("  ❌ MASSIVE_KEY is not set.")
        print("     Add it to GitHub Secrets (repository Settings → Secrets and variables → Actions).")
        output = {
            "fetched_at": ts,
            "error": "MASSIVE_KEY not configured",
            "snapshots": [],
            "candles": {},
            "options": {}
        }
        with open(OUTPUT, "w") as f:
            json.dump(output, f, indent=2)
        return

    errors = []

    # 1. Snapshots
    snapshots, snap_err = fetch_snapshots(TICKERS)
    if snap_err:
        errors.append(f"snapshots: {snap_err}")

    # 2. Candles — fetch for each ticker individually
    print("  Fetching daily candles (this may take a moment)...")
    candles_by_ticker = {}
    for ticker in TICKERS:
        candles, err = fetch_candles(ticker, days=90)
        if candles:
            candles_by_ticker[ticker] = candles
        elif err:
            errors.append(f"candles/{ticker}: {err}")
    print(f"  ✅ Got candles for {len(candles_by_ticker)}/{len(TICKERS)} tickers")

    # 3. Options chains — only for key tickers
    print("  Fetching options chains...")
    options_by_ticker = {}
    for ticker in OPTIONS_TICKERS:
        contracts, err = fetch_options_chain(ticker)
        if contracts:
            options_by_ticker[ticker] = contracts
            print(f"    {ticker}: {len(contracts)} contracts")
        elif err:
            print(f"    {ticker}: ❌ {err}")
            errors.append(f"options/{ticker}: {err}")
    print(f"  ✅ Options fetched for {len(options_by_ticker)}/{len(OPTIONS_TICKERS)} tickers")

    # 4. Save output
    output = {
        "fetched_at":  ts,
        "source":      "massive",
        "ticker_count": len(snapshots),
        "errors":      errors if errors else None,
        "snapshots":   snapshots,
        "candles":     candles_by_ticker,
        "options":     options_by_ticker
    }

    with open(OUTPUT, "w") as f:
        json.dump(output, f, indent=2)

    size_kb = round(os.path.getsize(OUTPUT) / 1024, 1)
    print(f"\n  ✅ Saved massive_data.json ({size_kb} KB)")

    if errors:
        print(f"\n  ⚠  {len(errors)} errors (partial data saved):")
        for e in errors:
            print(f"     • {e}")
    else:
        print("  ✅ No errors — full data set")

    print("=" * 60)
    print("")


if __name__ == "__main__":
    run()
