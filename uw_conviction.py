#!/usr/bin/env python3
"""
BASANI — Unusual Whales Conviction Layer (Phase 2)

For each scanner ticker, computes 5 smart-money conviction dimensions using UW API:
  1. iv_rank             — IV regime (penalize >70, boost <30)
  2. gex_levels          — Call wall / put wall / gamma flip = magnet zones
  3. flow_per_expiry     — Premium-weighted bullish/bearish flow by expiry
  4. darkpool            — Today's NBBO-positive (or negative) prints
  5. insider_ticker_flow — Recent insider buy vs sell pressure

Plus a market-wide context layer (called once, reused for all tickers):
  - market_tide          — Today's cumulative call/put premium sign
  - market_insider       — Today's total insider purchases vs sells

Why these specifically:
  - gex_levels give LITERAL magnet prices — entries near gamma_flip/gamma_magnet
    have institutional hedging support, exits near call_wall/put_wall don't.
  - flow_per_expiry ASK-side vs BID-side premium is the most predictive direction
    signal in the API: ask-side = people lifting the offer = aggressive buyers.
  - darkpool prints above NBBO midpoint = institutional accumulation confirmed.
  - insider flow directional (buy_sell column) is qualitative edge.
  - IV rank picks WHERE to position in the option chain (cheap → buy longer DTE,
    expensive → skip or shorter DTE).

Caching:
  - 4h per-ticker cache (matches one trading session — re-pull next morning)
  - 30m market tide cache (more volatile, refresh during session)

Output: /home/client_4319_1/basani_live/uw_conviction.json
        Format: { "generated": ISO, "market": {...}, "tickers": { "NVDA": {...} } }
"""

import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR       = Path(__file__).parent.resolve()
CACHE_DIR      = BASE_DIR / "uw_cache"
CACHE_DIR.mkdir(exist_ok=True)
OUTPUT_FILE    = BASE_DIR / "uw_conviction.json"
PER_TICKER_TTL = 4 * 3600   # 4h
MARKET_TIDE_TTL = 30 * 60   # 30m
UW_BASE        = "https://api.unusualwhales.com"


# ── AUTH + HTTP ───────────────────────────────────────────────────────────────
def _token():
    t = os.environ.get("UW_TOKEN", "")
    if not t:
        try:
            with open(BASE_DIR / ".env") as f:
                for line in f:
                    if line.startswith("UW_TOKEN"):
                        t = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except Exception:
            pass
    return t


def _get(path, params=None, timeout=15):
    url = UW_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + _token(),
        "Accept":        "application/json",
        "User-Agent":    "BASANI-phase2/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), r.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode() or "{}"), e.code
        except Exception:
            return {}, e.code
    except Exception as e:
        return {"error": str(e)}, 0


# ── CACHE ──────────────────────────────────────────────────────────────────────
def _cache_path(ticker, kind):
    return CACHE_DIR / f"{ticker}_{kind}.json"


def _read_cache(path, ttl):
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime > ttl:
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _write_cache(path, data):
    try:
        path.write_text(json.dumps(data, indent=2, default=str))
    except Exception as e:
        print(f"  WARN cache write {path.name}: {e}")


# ── IV RANK ───────────────────────────────────────────────────────────────────
def fetch_iv_rank(ticker):
    """Most recent IV rank reading for ticker."""
    cp = _cache_path(ticker, "iv")
    cached = _read_cache(cp, PER_TICKER_TTL)
    if cached is not None:
        return cached

    data, status = _get(f"/api/stock/{ticker}/iv-rank")
    if status != 200 or not isinstance(data, dict):
        return None

    rows = data.get("data", [])
    if not rows:
        return None
    latest = rows[0]

    out = {
        "iv_rank_1y": float(latest.get("iv_rank_1y", 0) or 0),
        "volatility": float(latest.get("volatility", 0) or 0),
        "date":       latest.get("date"),
    }
    _write_cache(cp, out)
    return out


# ── GEX LEVELS ────────────────────────────────────────────────────────────────
def fetch_gex_levels(ticker):
    """
    Returns dealer hedging magnets:
      call_wall:    highest call OI strike = resistance magnet (above)
      put_wall:     highest put OI strike = support magnet (below)
      gamma_flip:   dealer hedging mode changes here (price magnet)
      gamma_magnet: price gravitates here
    """
    cp = _cache_path(ticker, "gex")
    cached = _read_cache(cp, PER_TICKER_TTL)
    if cached is not None:
        return cached

    data, status = _get(f"/api/stock/{ticker}/gex-levels")
    if status != 200 or not isinstance(data, dict):
        return None
    payload = data.get("data") or data
    if not isinstance(payload, dict):
        return None

    out = {
        "call_wall":    float(payload.get("call_wall", 0) or 0),
        "put_wall":     float(payload.get("put_wall", 0) or 0),
        "gamma_flip":   float(payload.get("gamma_flip", 0) or 0),
        "gamma_magnet": float(payload.get("gamma_magnet", 0) or 0),
    }
    _write_cache(cp, out)
    return out


# ── FLOW PER EXPIRY ───────────────────────────────────────────────────────────
def fetch_flow_per_expiry(ticker):
    """
    Pull all-expiry flow for the ticker.
    Return today + nearest weekly + next weekly, with net call/put premium.
    """
    cp = _cache_path(ticker, "flow_expiry")
    cached = _read_cache(cp, PER_TICKER_TTL)
    if cached is not None:
        return cached

    data, status = _get(f"/api/stock/{ticker}/flow-per-expiry")
    if status != 200:
        return None
    # Endpoint returns BARE list (not wrapped in {"data": ...})
    rows = data if isinstance(data, list) else (data.get("data", []) if isinstance(data, dict) else [])
    if not isinstance(rows, list):
        return None

    today = datetime.now(timezone.utc).date().isoformat()
    today_rows = [r for r in rows if (r.get("date") or "").startswith(today)]

    call_prem = sum(float(r.get("call_premium", 0) or 0) for r in today_rows)
    put_prem  = sum(float(r.get("put_premium", 0) or 0)  for r in today_rows)
    call_ask  = sum(float(r.get("call_premium_ask_side", 0) or 0) for r in today_rows)
    put_bid   = sum(float(r.get("put_premium_ask_side", 0) or 0)  for r in today_rows)

    net = call_prem - put_prem
    bullish_ratio = (call_prem / (call_prem + put_prem)) if (call_prem + put_prem) > 0 else None

    out = {
        "date":               today,
        "call_premium":       round(call_prem, 2),
        "put_premium":        round(put_prem, 2),
        "net_premium":        round(net, 2),
        "call_premium_ask":   round(call_ask, 2),   # aggressive call buying
        "put_premium_ask":    round(put_bid, 2),    # aggressive put buying
        "bullish_ratio":      round(bullish_ratio, 3) if bullish_ratio is not None else None,
        "expiry_count_today": len(today_rows),
    }
    _write_cache(cp, out)
    return out


# ── DARK POOL (per-ticker endpoint) ──────────────────────────────────────────
def fetch_darkpool(ticker):
    """
    Use /api/darkpool/{ticker} (which returns up to 500 prints for today).
    Compute NBBO-positive ratio + total premium + large print count.
    """
    cp = _cache_path(ticker, "dp")
    cached = _read_cache(cp, PER_TICKER_TTL)
    if cached is not None:
        return cached

    data, status = _get(f"/api/darkpool/{ticker}")
    if status != 200 or not isinstance(data, dict):
        return None
    rows = data.get("data", [])
    if not isinstance(rows, list):
        return None

    today = datetime.now(timezone.utc).date().isoformat()
    today_rows = [
        r for r in rows
        if r.get("executed_at", "").startswith(today) and not r.get("canceled")
    ]

    total_prem = 0.0
    nbbo_pos = 0
    nbbo_neg = 0
    large_prints = 0

    for r in today_rows:
        prem = float(r.get("premium", 0) or 0)
        total_prem += prem
        if prem >= 500_000:
            large_prints += 1
        try:
            bid = float(r.get("nbbo_bid", 0) or 0)
            ask = float(r.get("nbbo_ask", 0) or 0)
            mid = (bid + ask) / 2
            trd = float(r.get("price", 0) or 0)
            if mid > 0:
                if trd > mid:
                    nbbo_pos += 1
                elif trd < mid:
                    nbbo_neg += 1
        except Exception:
            continue

    total_nbbo = nbbo_pos + nbbo_neg
    ratio = (nbbo_pos / total_nbbo) if total_nbbo > 0 else None

    out = {
        "date":               today,
        "print_count":        len(today_rows),
        "total_premium":      round(total_prem, 2),
        "nbbo_positive":      nbbo_pos,
        "nbbo_negative":      nbbo_neg,
        "nbbo_positive_ratio": round(ratio, 3) if ratio is not None else None,
        "large_prints":       large_prints,
    }
    _write_cache(cp, out)
    return out


# ── INSIDER TICKER FLOW ───────────────────────────────────────────────────────
def fetch_insider_flow(ticker):
    """
    Pull insider buy/sell pressure for this ticker.
    Aggregate last 90 days into net buy-sell pressure.
    """
    cp = _cache_path(ticker, "insider")
    cached = _read_cache(cp, PER_TICKER_TTL)
    if cached is not None:
        return cached

    data, status = _get(f"/api/insider/{ticker}/ticker-flow")
    if status != 200:
        return None
    # Endpoint may return bare list or wrapped dict
    rows = data if isinstance(data, list) else (data.get("data", []) if isinstance(data, dict) else [])
    if not isinstance(rows, list) or not rows:
        out = {"buy_premium": 0, "sell_premium": 0, "net_premium": 0, "buy_count": 0, "sell_count": 0}
        _write_cache(cp, out)
        return out

    # Aggregate over last 90 days (insider filings are sparse)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).date().isoformat()
    recent = [r for r in rows if (r.get("date") or "") >= cutoff] or rows[:30]

    buy_prem = sum(float(r.get("premium", 0) or 0) for r in recent if r.get("buy_sell", "").lower() == "buy")
    sell_prem = sum(float(r.get("premium", 0) or 0) for r in recent if r.get("buy_sell", "").lower() == "sell")
    buy_count = sum(1 for r in recent if r.get("buy_sell", "").lower() == "buy")
    sell_count = sum(1 for r in recent if r.get("buy_sell", "").lower() == "sell")
    net = buy_prem + sell_prem   # sell_prem is already negative

    out = {
        "buy_premium":  round(buy_prem, 2),
        "sell_premium": round(sell_prem, 2),
        "net_premium":  round(net, 2),
        "buy_count":    buy_count,
        "sell_count":   sell_count,
        "rows":         len(rows),
    }
    _write_cache(cp, out)
    return out


# ── MARKET TIDE ───────────────────────────────────────────────────────────────
def fetch_market_tide():
    cp = CACHE_DIR / "market_tide.json"
    cached = _read_cache(cp, MARKET_TIDE_TTL)
    if cached is not None:
        return cached

    data, status = _get("/api/market/market-tide")
    if status != 200 or not isinstance(data, dict):
        return None
    rows = data.get("data", [])
    if not rows:
        return None

    today = datetime.now(timezone.utc).date().isoformat()
    today_rows = [r for r in rows if r.get("date") == today]

    cum_call = sum(float(r.get("net_call_premium", 0) or 0) for r in today_rows)
    cum_put  = sum(float(r.get("net_put_premium", 0) or 0)  for r in today_rows)

    out = {
        "date":                 today,
        "cum_call_premium":     round(cum_call, 2),
        "cum_put_premium":      round(cum_put, 2),
        "cum_net_premium":      round(cum_call + cum_put, 2),
        "bar_count_today":      len(today_rows),
    }
    _write_cache(cp, out)
    return out


# ── MARKET INSIDER ────────────────────────────────────────────────────────────
def fetch_market_insider():
    cp = CACHE_DIR / "market_insider.json"
    cached = _read_cache(cp, MARKET_TIDE_TTL)
    if cached is not None:
        return cached

    data, status = _get("/api/market/insider-buy-sells")
    if status != 200 or not isinstance(data, dict):
        return None
    rows = data.get("data", [])
    if not rows:
        return None

    today = datetime.now(timezone.utc).date().isoformat()
    today_rows = [r for r in rows if r.get("filing_date") == today]
    if not today_rows:
        today_rows = rows[:5]   # fallback to most recent days

    tot_buy_notional  = sum(float(r.get("purchases_notional", 0) or 0) for r in today_rows)
    tot_sell_notional = sum(float(r.get("sells_notional", 0) or 0)      for r in today_rows)

    out = {
        "today":              today,
        "buy_notional":       round(tot_buy_notional, 2),
        "sell_notional":      round(tot_sell_notional, 2),
        "net_notional":       round(tot_buy_notional + tot_sell_notional, 2),  # sell is already negative
        "rows_used":          len(today_rows),
    }
    _write_cache(cp, out)
    return out


# ── COMBINE FOR ONE TICKER ────────────────────────────────────────────────────
def fetch_ticker_conviction(ticker, market=None):
    """
    Pull all per-ticker signals + market context, return flat dict for scanner.
    """
    iv    = fetch_iv_rank(ticker)
    gex   = fetch_gex_levels(ticker)
    flow  = fetch_flow_per_expiry(ticker)
    dp    = fetch_darkpool(ticker)
    ins   = fetch_insider_flow(ticker)

    # Build human-readable signal tags
    sigs = []

    if iv:
        if iv["iv_rank_1y"] < 20:
            sigs.append("IV_CHEAP")
        elif iv["iv_rank_1y"] > 70:
            sigs.append("IV_EXPENSIVE")

    if flow:
        net = flow.get("net_premium") or 0
        if net > 5_000_000:
            sigs.append("FLOW_BULL_5M+")
        elif net > 1_000_000:
            sigs.append("FLOW_BULL")
        elif net < -5_000_000:
            sigs.append("FLOW_BEAR_5M+")
        elif net < -1_000_000:
            sigs.append("FLOW_BEAR")

    if dp:
        ratio = dp.get("nbbo_positive_ratio")
        prints = dp.get("print_count", 0)
        if prints >= 3:
            if ratio is not None and ratio > 0.65:
                sigs.append("DP_BUYERS")
            elif ratio is not None and ratio < 0.35:
                sigs.append("DP_SELLERS")
        if dp.get("large_prints", 0) >= 2:
            sigs.append("DP_INSTITUTIONAL")

    if ins:
        net_ins = ins.get("net_premium", 0)
        if net_ins > 500_000:
            sigs.append("INSIDER_BUY")
        elif net_ins < -10_000_000:
            sigs.append("INSIDER_SELL")

    if market:
        net_market = market.get("cum_net_premium", 0)
        if net_market > 50_000_000:
            sigs.append("MKT_RISK_ON")
        elif net_market < -50_000_000:
            sigs.append("MKT_RISK_OFF")

    return {
        "ticker":               ticker,
        "iv_rank":              iv["iv_rank_1y"] if iv else None,
        "vol_annual":           iv["volatility"] if iv else None,
        "gex_call_wall":        gex["call_wall"] if gex else None,
        "gex_put_wall":         gex["put_wall"] if gex else None,
        "gex_gamma_flip":       gex["gamma_flip"] if gex else None,
        "gex_gamma_magnet":     gex["gamma_magnet"] if gex else None,
        "flow_net_premium":     flow["net_premium"] if flow else 0,
        "flow_call_ask_prem":   flow["call_premium_ask"] if flow else 0,
        "flow_put_ask_prem":    flow["put_premium_ask"]  if flow else 0,
        "flow_bullish_ratio":   flow["bullish_ratio"] if flow else None,
        "dp_print_count":       dp["print_count"]    if dp else 0,
        "dp_total_premium":     dp["total_premium"]  if dp else 0,
        "dp_nbbo_ratio":        dp["nbbo_positive_ratio"] if dp else None,
        "dp_large_prints":      dp["large_prints"]   if dp else 0,
        "insider_net_premium":  ins["net_premium"]   if ins else 0,
        "signals":              sigs,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI:    python3 uw_conviction.py NVDA AAPL AMD
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    DEFAULT = ["NVDA", "AAPL", "AMD", "META", "MSFT", "SPY", "QQQ", "TSLA", "NFLX", "GOOGL"]
    tickers = sys.argv[1:] or DEFAULT

    print(f"\n{'=' * 75}")
    print(f"  UW Conviction — {len(tickers)} tickers + market context")
    print(f"{'=' * 75}")

    market     = fetch_market_tide()
    mkt_ins    = fetch_market_insider()

    if market:
        net = market["cum_net_premium"]
        direction = "RISK_ON" if net > 50_000_000 else ("RISK_OFF" if net < -50_000_000 else "NEUTRAL")
        print(f"  Market tide:   net ${net/1_000_000:+.1f}M  →  {direction}")
    if mkt_ins:
        print(f"  Insider mkts:  buy ${mkt_ins['buy_notional']/1_000_000:+.1f}M / sell ${mkt_ins['sell_notional']/1_000_000:+.1f}M")

    print(f"\n  {'TIC':5} {'IV':>3} {'FlowNet':>10} {'DP':>7} {'NBBO+':>6} {'Insider':>10}  Tags")
    print(f"  {'---':5} {'--':>3} {'-------':>10} {'--':>7} {'-----':>6} {'-------':>10}  ----")

    results = {}
    for t in tickers:
        t = t.upper()
        c = fetch_ticker_conviction(t, market=market)
        results[t] = c
        iv_r = f"{c['iv_rank']:.0f}" if c['iv_rank'] is not None else "N/A"
        flow_d = c['flow_net_premium'] / 1_000_000
        dp_p = c['dp_total_premium'] / 1_000_000
        nbbo = f"{c['dp_nbbo_ratio']:.0%}" if c['dp_nbbo_ratio'] is not None else "N/A"
        ins = c['insider_net_premium'] / 1_000_000
        tags = ", ".join(c["signals"]) if c["signals"] else "(none)"
        print(f"  {t:5} {iv_r:>3} ${flow_d:+5.2f}M  ${dp_p:5.2f}M {nbbo:>6} ${ins:+6.2f}M   {tags}")

    OUTPUT_FILE.write_text(json.dumps({
        "generated":   datetime.now(timezone.utc).isoformat(),
        "market":      market,
        "market_ins":  mkt_ins,
        "tickers":     results,
    }, indent=2, default=str))
    print(f"\n  Saved → {OUTPUT_FILE}")
