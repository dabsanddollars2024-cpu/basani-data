#!/usr/bin/env python3
"""
BASANI Automated Market Scanner
================================
Runs every 30 min during market hours via GitHub Actions (Mon–Fri 9:30am–4:00pm ET).
Pulls live data from Unusual Whales + Alpaca, computes scores/signals,
then upserts results into Supabase `basani_data` table so the dashboard
at https://exquisite-duckanoo-37f64d.netlify.app/ always shows fresh data.

Supabase table schema (basani_data):
  key          text  PRIMARY KEY
  payload      jsonb
  refreshed_at timestamptz DEFAULT now()

Keys written: 'scan', 'plays', 'news', 'whales'
"""

import json
import os
import sys
import time
import math
import random
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta, date

# ── Credentials (injected by GitHub Actions secrets) ────────────────────────────
UW_TOKEN       = os.environ.get("UW_TOKEN",          "6c2959a5-ebef-4945-9c2b-5be1c81e0d57")
ALPACA_KEY     = os.environ.get("ALPACA_API_KEY",    "PKVAR7JKCKGN63MV7OCMRQOSCO")
ALPACA_SECRET  = os.environ.get("ALPACA_SECRET_KEY", "2zauQm6Ddh6sq1hPn9BuMW6T3A8uRJ8d2yeJQ9mLBkai")
SUPABASE_URL   = os.environ.get("SUPABASE_URL",      "https://lsvbhlgxeddssgdpvwtq.supabase.co")
# Falls back to anon key (which has write access — RLS is open on basani_data)
_SUPABASE_ANON = "sb_publishable_dy77qQrBEFQjC-fQLgHcyA_6iX3krzo"
SUPABASE_KEY   = os.environ.get("SUPABASE_SERVICE_KEY", _SUPABASE_ANON)

# ── Watchlist ───────────────────────────────────────────────────────────────────
WATCHLIST = [
    "SPY", "QQQ", "NVDA", "AMD", "TSLA", "AAPL", "MSFT", "META",
    "AMZN", "GOOGL", "PLTR", "COIN", "MU", "INTC", "AVGO", "QCOM",
    "ORCL", "DIS", "GLD", "XLE", "XLF", "TLT",
]

# ── API base URLs ───────────────────────────────────────────────────────────────
ALPACA_DATA  = "https://data.alpaca.markets"
ALPACA_PAPER = "https://paper-api.alpaca.markets"
UW_BASE      = "https://api.unusualwhales.com"

# ─────────────────────────────────────────────────────────────────────────────────
# HTTP HELPERS
# ─────────────────────────────────────────────────────────────────────────────────

def http_get(url, headers, timeout=20):
    """GET → parsed JSON. Returns (data, status_code)."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body), e.code
        except Exception:
            return {"error": body[:200]}, e.code
    except Exception as e:
        return {"error": str(e)}, 0


def http_post(url, headers, body_dict, timeout=20):
    """POST JSON → parsed JSON. Returns (data, status_code)."""
    data = json.dumps(body_dict).encode("utf-8")
    headers = {**headers, "Content-Type": "application/json", "Content-Length": str(len(data))}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return (json.loads(raw) if raw else {}), r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body), e.code
        except Exception:
            return {"error": body[:200]}, e.code
    except Exception as e:
        return {"error": str(e)}, 0


def alpaca_get(path, params=None):
    url = ALPACA_DATA + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    return http_get(url, headers)


def uw_get(path, params=None):
    url = UW_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Authorization": f"Bearer {UW_TOKEN}",
        "Accept":        "application/json",
        "User-Agent":    "BASANI/2.0",
    }
    return http_get(url, headers)


# ─────────────────────────────────────────────────────────────────────────────────
# ALPACA — PRICE SNAPSHOTS
# ─────────────────────────────────────────────────────────────────────────────────

def fetch_snapshots(symbols):
    """Fetch latest trade/quote/bar for all symbols in one call."""
    data, status = alpaca_get("/v2/stocks/snapshots", {
        "symbols": ",".join(symbols),
        "feed":    "iex",
    })
    if status != 200:
        print(f"  [Alpaca] snapshots failed ({status}): {data}")
        return {}

    result = {}
    for sym in symbols:
        snap = data.get(sym)
        if not snap:
            continue
        trade = snap.get("latestTrade", {})
        quote = snap.get("latestQuote", {})
        daily = snap.get("dailyBar", {})
        prev  = snap.get("prevDailyBar", {})

        price      = trade.get("p") or daily.get("c") or 0
        prev_close = prev.get("c") or 0
        chg_pct    = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0

        result[sym] = {
            "price":      round(float(price), 2),
            "prev_close": round(float(prev_close), 2),
            "chg_pct":    chg_pct,
            "bid":        float(quote.get("bp", 0)),
            "ask":        float(quote.get("ap", 0)),
            "volume":     int(daily.get("v", 0)),
            "open":       float(daily.get("o", 0)),
            "high":       float(daily.get("h", 0)),
            "low":        float(daily.get("l", 0)),
            "vwap":       float(daily.get("vw", 0)),
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────────
# ALPACA — DAILY BARS (for RSI + SMA)
# ─────────────────────────────────────────────────────────────────────────────────

def fetch_bars(symbols, days=60):
    """Fetch last N days of daily OHLCV for all symbols."""
    end   = date.today()
    start = end - timedelta(days=days + 10)
    data, status = alpaca_get("/v2/stocks/bars", {
        "symbols":    ",".join(symbols),
        "timeframe":  "1Day",
        "start":      start.isoformat(),
        "end":        end.isoformat(),
        "limit":      10000,
        "feed":       "iex",
        "sort":       "asc",
        "adjustment": "raw",
    })
    if status != 200:
        print(f"  [Alpaca] bars failed ({status})")
        return {}
    return data.get("bars", {})


def calc_rsi(closes, period=14):
    """Standard Wilder RSI."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def calc_sma(closes, period):
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 2)


# ─────────────────────────────────────────────────────────────────────────────────
# SCORING ENGINE
# ─────────────────────────────────────────────────────────────────────────────────

def score_ticker(snap, bars):
    """
    Compute a 0–100 bullish momentum score from price action.
    Returns dict with score, direction, rsi, sma20, sma50, signals, stop,
    cons_target, agg_target.
    """
    closes = [b["c"] for b in bars] if bars else []
    price  = snap["price"]
    chg    = snap["chg_pct"]
    vol    = snap["volume"]

    signals = []
    pts     = 50  # start neutral

    # ── RSI ──────────────────────────────────────────────────────────────────────
    rsi = calc_rsi(closes) if closes else None
    if rsi is not None:
        if rsi < 30:
            pts += 8; signals.append(f"RSI oversold {rsi}")
        elif rsi > 70:
            pts -= 8; signals.append(f"RSI overbought {rsi}")
        elif 45 <= rsi <= 60:
            pts += 5; signals.append(f"RSI healthy {rsi}")

    # ── SMA stack ────────────────────────────────────────────────────────────────
    sma20 = calc_sma(closes, 20)
    sma50 = calc_sma(closes, 50)

    if sma20 and price > sma20:
        pts += 10; signals.append("Above SMA20")
    elif sma20:
        pts -= 10; signals.append("Below SMA20")

    if sma50 and price > sma50:
        pts += 8; signals.append("Above SMA50")
    elif sma50:
        pts -= 8; signals.append("Below SMA50")

    if sma20 and sma50 and sma20 > sma50:
        pts += 5; signals.append("SMA20 > SMA50 (bullish stack)")

    # ── Price momentum ───────────────────────────────────────────────────────────
    if chg > 3:
        pts += 12; signals.append(f"+{chg:.1f}% today (strong)")
    elif chg > 1:
        pts += 6; signals.append(f"+{chg:.1f}% today")
    elif chg < -3:
        pts -= 12; signals.append(f"{chg:.1f}% today (weak)")
    elif chg < -1:
        pts -= 6; signals.append(f"{chg:.1f}% today")

    # ── Volume confirmation ───────────────────────────────────────────────────────
    if len(closes) >= 20 and bars:
        avg_vol = sum(b.get("v", 0) for b in bars[-20:]) / 20
        if avg_vol > 0 and vol > avg_vol * 1.5 and chg > 0:
            pts += 5; signals.append("High volume breakout")
        elif avg_vol > 0 and vol < avg_vol * 0.5:
            pts -= 3; signals.append("Low volume")

    # ── Clamp ────────────────────────────────────────────────────────────────────
    score = max(0, min(100, pts))
    direction = "BULLISH" if score >= 65 else "BEARISH" if score <= 35 else "NEUTRAL"

    # ── Price targets ─────────────────────────────────────────────────────────────
    atr = None
    if len(closes) >= 14:
        highs = [b.get("h", b["c"]) for b in bars[-14:]]
        lows  = [b.get("l", b["c"]) for b in bars[-14:]]
        trs   = [highs[i] - lows[i] for i in range(14)]
        atr   = sum(trs) / 14

    stop        = round(price * 0.95, 2) if atr is None else round(price - 1.5 * atr, 2)
    cons_target = round(price * 1.05, 2) if atr is None else round(price + 1.5 * atr, 2)
    agg_target  = round(price * 1.10, 2) if atr is None else round(price + 3.0 * atr, 2)

    return {
        "score":       score,
        "direction":   direction,
        "rsi":         rsi,
        "sma20":       sma20,
        "sma50":       sma50,
        "signals":     signals[:5],
        "stop":        stop,
        "cons_target": cons_target,
        "agg_target":  agg_target,
    }


# ─────────────────────────────────────────────────────────────────────────────────
# MARKET REGIME
# ─────────────────────────────────────────────────────────────────────────────────

def derive_regime(tickers_data, uw_flow):
    """Determine overall market regime from SPY/QQQ scores + UW flow ratio."""
    spy_ticker = next((t for t in tickers_data if t["ticker"] == "SPY"), None)
    qqq_ticker = next((t for t in tickers_data if t["ticker"] == "QQQ"), None)

    spy_score = spy_ticker["score"] if spy_ticker else 50
    qqq_score = qqq_ticker["score"] if qqq_ticker else 50
    avg_score = (spy_score + qqq_score) / 2

    # Factor in UW call/put ratio
    calls = sum(1 for a in uw_flow if a.get("side") == "bullish")
    puts  = sum(1 for a in uw_flow if a.get("side") == "bearish")
    total = calls + puts
    uw_call_pct = calls / total if total > 0 else 0.5

    # Blend: 70% price action, 30% UW flow
    blended = avg_score * 0.7 + uw_call_pct * 100 * 0.3

    if blended >= 62:
        return "BULLISH BIAS"
    elif blended <= 38:
        return "BEARISH"
    else:
        return "NEUTRAL"


# ─────────────────────────────────────────────────────────────────────────────────
# UNUSUAL WHALES — FLOW ALERTS
# ─────────────────────────────────────────────────────────────────────────────────

def fetch_uw_flow():
    """Pull latest options flow alerts from Unusual Whales."""
    items = []
    MIN_PREM = 10_000

    for path, params in [
        ("/api/option-trades/flow-alerts", {"limit": "200"}),
        ("/api/flow/alerts",               {"limit": "200"}),
    ]:
        data, status = uw_get(path, params)
        if status == 200:
            print(f"  [UW] flow → {path} ✅")
            break
        print(f"  [UW] flow skip {path} ({status})")
    else:
        return items

    def fmt_prem(v):
        try:
            v = float(str(v).replace(",", ""))
            if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
            if v >= 1_000:     return f"${v/1_000:.0f}K"
            return f"${v:.0f}"
        except Exception:
            return str(v)

    rows = data if isinstance(data, list) else data.get("data", [])
    now  = datetime.now(timezone.utc)

    for row in rows:
        try:
            ticker   = (row.get("ticker") or row.get("symbol") or "").upper()
            premium  = row.get("total_premium") or row.get("premium") or 0
            strike   = row.get("strike") or row.get("strike_price") or ""
            expiry   = row.get("expiry") or row.get("expiration_date") or ""
            side     = (row.get("sentiment") or row.get("side") or row.get("call_put") or "").lower()
            opt_type = (row.get("option_type") or row.get("call_put") or "").upper()
            ts       = row.get("created_at") or row.get("timestamp") or now.isoformat()
            uid      = str(row.get("id") or row.get("trade_id") or ts + ticker)

            try:
                prem_val = float(str(premium).replace(",", ""))
            except Exception:
                prem_val = 0
            if prem_val < MIN_PREM:
                continue

            if "bull" in side or side == "call" or opt_type == "C":
                side_norm = "bullish"
            elif "bear" in side or side == "put" or opt_type == "P":
                side_norm = "bearish"
            else:
                side_norm = side or "neutral"

            items.append({
                "id":       uid,
                "timestamp": ts,
                "ticker":   ticker,
                "strike":   str(strike),
                "expiry":   str(expiry)[:10],
                "premium":  fmt_prem(prem_val),
                "side":     side_norm,
                "type":     "options_flow",
                "source":   "unusual_whales",
                "flagged":  prem_val >= 500_000,
                "channel":  "flow-alerts",
                "raw":      f"{ticker} {opt_type}{strike} {expiry} {fmt_prem(prem_val)} {side_norm}",
            })
        except Exception:
            continue

    return items


def fetch_uw_darkpool():
    """Pull recent dark pool prints."""
    items = []
    data, status = uw_get("/api/darkpool/recent", {"limit": "50"})
    if status != 200:
        return items

    rows = data if isinstance(data, list) else data.get("data", [])
    now  = datetime.now(timezone.utc)

    for row in rows:
        try:
            ticker  = (row.get("ticker") or "").upper()
            premium = row.get("premium") or row.get("notional") or 0
            ts      = row.get("executed_at") or row.get("timestamp") or now.isoformat()
            uid     = str(row.get("id") or ts + ticker + "dp")
            try:
                prem_val = float(str(premium).replace(",", ""))
            except Exception:
                prem_val = 0
            if prem_val < 10_000:
                continue
            items.append({
                "id":       uid,
                "timestamp": ts,
                "ticker":   ticker,
                "strike":   "",
                "expiry":   "",
                "premium":  f"${prem_val/1000:.0f}K" if prem_val < 1_000_000 else f"${prem_val/1_000_000:.1f}M",
                "side":     "dark pool",
                "type":     "dark_pool",
                "source":   "unusual_whales",
                "flagged":  prem_val >= 1_000_000,
                "channel":  "dark-pool",
                "raw":      f"🌑 DARK POOL {ticker}",
            })
        except Exception:
            continue
    return items


# ─────────────────────────────────────────────────────────────────────────────────
# AUTO-GENERATE PLAYS FROM LIVE SCAN
# ─────────────────────────────────────────────────────────────────────────────────

def generate_plays(tickers_data, uw_flow, scan_time_str):
    """
    Auto-generate options plays based on live score + UW flow consensus.
    Returns list in PLAYS_LOG format.
    """
    plays = []

    # UW flow call-side heavyweights
    uw_bullish = {}
    for a in uw_flow:
        if a.get("side") == "bullish":
            t = a.get("ticker", "")
            uw_bullish[t] = uw_bullish.get(t, 0) + 1

    play_id = 1
    today = date.today()

    for ticker_data in tickers_data:
        sym   = ticker_data["ticker"]
        score = ticker_data["score"]
        price = ticker_data["price"]
        dir_  = ticker_data["direction"]

        if sym in ("SPY", "QQQ", "GLD", "XLE", "XLF", "TLT"):
            continue  # skip ETFs from individual plays list

        uw_hits = uw_bullish.get(sym, 0)
        conf_score = score + uw_hits * 5

        if dir_ == "BULLISH" and conf_score >= 70:
            # Nearest weekly expiry (Friday)
            days_to_friday = (4 - today.weekday()) % 7
            if days_to_friday == 0:
                days_to_friday = 7
            expiry = today + timedelta(days=days_to_friday)

            # ATM call
            atm_strike = round(price / 5) * 5  # round to nearest $5
            if atm_strike < price:
                atm_strike += 5

            confidence = "HIGH" if conf_score >= 85 else "MEDIUM"
            conf_color = "#2e8b57" if confidence == "HIGH" else "#b8860b"

            signals_text = " · ".join(ticker_data.get("signals", [])[:2]) or f"Score {score}"
            uw_note = f" · {uw_hits}x UW flow" if uw_hits > 0 else ""

            plays.append({
                "id":            f"AUTO-{play_id:03d}",
                "ticker":        sym,
                "type":          "CALL",
                "strike":        atm_strike,
                "expiry":        expiry.isoformat(),
                "date_suggested": scan_time_str[:10],
                "status":        "open",
                "grade":         "—",
                "est_option_mid": None,
                "exit_price":    None,
                "pnl_pct":       None,
                "confidence":    confidence,
                "conf_color":    conf_color,
                "why":           f"Score {score} · {signals_text}{uw_note}",
                "entry":         price,
                "stop":          ticker_data.get("stop"),
                "target":        ticker_data.get("cons_target"),
            })
            play_id += 1

        if play_id > 10:
            break

    return plays


# ─────────────────────────────────────────────────────────────────────────────────
# NEWS FEED — market summary from scan data
# ─────────────────────────────────────────────────────────────────────────────────

def build_news_feed(tickers_data, uw_flow, scan_time_str):
    """
    Build a structured news-feed payload the dashboard `buildLiveFeed` + Blubber can consume.
    Fields match what Blubber expects: title, url, source, impact_level, sentiment,
    timestamp, tickers (array), impact_score.
    """
    now_et = datetime.now(timezone(timedelta(hours=-4)))  # ET (rough)
    items  = []

    # ── Top movers from scan ──────────────────────────────────────────────────
    by_abs_chg = sorted(tickers_data, key=lambda t: abs(t.get("chg_pct", 0)), reverse=True)[:8]
    for t in by_abs_chg:
        chg = t["chg_pct"]
        arrow = "📈" if chg > 0 else "📉"
        abs_chg = abs(chg)
        impact_level = "HIGH" if abs_chg >= 3 else "MEDIUM" if abs_chg >= 1 else "LOW"
        signals = " · ".join(t.get("signals", [])[:3]) or f"Score {t['score']} {t['direction']}"
        items.append({
            "id":           f"scan-{t['ticker']}-{scan_time_str[:10]}",
            "timestamp":    scan_time_str,
            "title":        f"{arrow} {t['ticker']} {'+' if chg > 0 else ''}{chg:.1f}% — ${t['price']:.2f}  |  {signals}",
            "body":         signals,
            "url":          f"https://finance.yahoo.com/quote/{t['ticker']}",
            "ticker":       t["ticker"],
            "tickers":      [t["ticker"]],
            "source":       "BASANI Scanner",
            "sentiment":    "bullish" if chg > 0 else "bearish",
            "impact_level": impact_level,
            "impact_score": abs_chg,
            "type":         "market_move",
        })

    # ── Flagged UW flow (whale prints) ────────────────────────────────────────
    flagged = [a for a in uw_flow if a.get("flagged")][:5]
    for a in flagged:
        items.append({
            "id":           a["id"],
            "timestamp":    a["timestamp"],
            "title":        f"🐋 {a['ticker']} {a.get('premium','')} {a['side'].upper()} OPTIONS FLOW",
            "body":         a.get("raw", ""),
            "url":          f"https://unusualwhales.com/flow?symbol={a['ticker']}",
            "ticker":       a["ticker"],
            "tickers":      [a["ticker"]],
            "source":       "Unusual Whales",
            "sentiment":    a["side"],
            "impact_level": "HIGH",
            "impact_score": 10,
            "type":         "unusual_flow",
        })

    # ── High-volume UW flow (non-flagged but notable) ─────────────────────────
    non_flagged = [a for a in uw_flow if not a.get("flagged") and a.get("side") in ("bullish","bearish")][:5]
    for a in non_flagged:
        items.append({
            "id":           a["id"] + "-nf",
            "timestamp":    a["timestamp"],
            "title":        f"⚡ {a['ticker']} {a.get('premium','')} {a['side'].upper()} flow",
            "body":         a.get("raw", ""),
            "url":          f"https://unusualwhales.com/flow?symbol={a['ticker']}",
            "ticker":       a["ticker"],
            "tickers":      [a["ticker"]],
            "source":       "Unusual Whales",
            "sentiment":    a["side"],
            "impact_level": "MEDIUM",
            "impact_score": 5,
            "type":         "options_flow",
        })

    return {
        "generated":  scan_time_str,
        "scan_time":  scan_time_str,
        "items":      items,
        "count":      len(items),
    }


# ─────────────────────────────────────────────────────────────────────────────────
# SUPABASE WRITE
# ─────────────────────────────────────────────────────────────────────────────────

def supabase_upsert(key, payload):
    """Upsert a single row into basani_data."""
    if not SUPABASE_KEY:
        print(f"  [Supabase] ⚠ No SUPABASE_SERVICE_KEY — skipping upsert for '{key}'")
        return False

    url = f"{SUPABASE_URL}/rest/v1/basani_data"
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Prefer":        "resolution=merge-duplicates",
    }
    body = {
        "key":          key,
        "payload":      payload,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }
    data, status = http_post(url, headers, body)
    if status in (200, 201, 204):
        print(f"  [Supabase] ✅ upserted '{key}'")
        return True
    else:
        print(f"  [Supabase] ❌ failed '{key}' ({status}): {data}")
        return False


def save_json_fallback(key, payload):
    """Write payload to a local JSON file as fallback (for GitHub commit)."""
    filenames = {
        "scan":     "scan_output.json",
        "plays":    "plays_log.json",
        "news":     "news.json",
        "whales":   "unusual_whales.json",
        "calendar": "calendar.json",
    }
    fname = filenames.get(key, f"{key}.json")
    path  = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"  [File] wrote {fname}")


# ─────────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────────

def main():
    now_utc     = datetime.now(timezone.utc)
    scan_time   = now_utc.isoformat()
    et_offset   = timedelta(hours=-4)  # EDT (May–Nov); use -5 for EST
    now_et      = now_utc + et_offset
    scan_time_et = now_et.strftime("%Y-%m-%d %H:%M ET")

    print()
    print("=" * 60)
    print(f"  BASANI Auto-Scanner  —  {scan_time_et}")
    print("=" * 60)

    # ── 1. Unusual Whales flow ────────────────────────────────────────────────
    print("\n[1/4] Unusual Whales flow...")
    uw_flow = fetch_uw_flow()
    uw_dp   = fetch_uw_darkpool()
    all_uw  = uw_flow + uw_dp
    print(f"  flow={len(uw_flow)}  darkpool={len(uw_dp)}  total={len(all_uw)}")

    # ── 2. Alpaca snapshots ───────────────────────────────────────────────────
    print("\n[2/4] Alpaca snapshots...")
    snaps = fetch_snapshots(WATCHLIST)
    print(f"  received {len(snaps)}/{len(WATCHLIST)} snapshots")

    # ── 3. Alpaca bars for RSI/SMA ────────────────────────────────────────────
    print("\n[3/4] Alpaca bars (RSI + SMA)...")
    bars_all = fetch_bars(WATCHLIST, days=60)
    print(f"  received bars for {len(bars_all)} tickers")

    # ── 4. Score every ticker ─────────────────────────────────────────────────
    print("\n[4/4] Scoring tickers...")
    tickers_data = []
    for sym in WATCHLIST:
        snap = snaps.get(sym)
        if not snap:
            print(f"  {sym:<8} no snapshot — skip")
            continue
        bars = bars_all.get(sym, [])
        scores = score_ticker(snap, bars)
        entry = {
            "ticker":      sym,
            "price":       snap["price"],
            "chg_pct":     snap["chg_pct"],
            "volume":      snap["volume"],
            "open":        snap["open"],
            "high":        snap["high"],
            "low":         snap["low"],
            "vwap":        snap["vwap"],
            **scores,
        }
        tickers_data.append(entry)
        dir_icon = "🟢" if scores["direction"] == "BULLISH" else "🔴" if scores["direction"] == "BEARISH" else "⚪"
        print(f"  {sym:<8} ${snap['price']:>8.2f}  {snap['chg_pct']:>+6.2f}%  score={scores['score']:>3}  {dir_icon} {scores['direction']}")

    # Sort by score desc
    tickers_data.sort(key=lambda t: t["score"], reverse=True)

    # ── Derive regime ─────────────────────────────────────────────────────────
    regime = derive_regime(tickers_data, uw_flow)
    spy_snap = snaps.get("SPY", {})
    qqq_snap = snaps.get("QQQ", {})
    print(f"\n  Regime: {regime}  |  SPY=${spy_snap.get('price',0):.2f}  QQQ=${qqq_snap.get('price',0):.2f}")

    # ── Build summary ─────────────────────────────────────────────────────────
    top = tickers_data[0] if tickers_data else {}
    bearish_list = [t["ticker"] for t in tickers_data if t["direction"] == "BEARISH"]
    summary = (
        f"Market is showing a <strong>{regime}</strong> tone as of {scan_time_et}. "
        f"Top signal: <strong>{top.get('ticker','—')}</strong> "
        f"(score {top.get('score','?')}, "
        f"{'+' if top.get('chg_pct',0)>0 else ''}{top.get('chg_pct',0):.1f}%, "
        f"{top.get('direction','?')}). "
        + (f"Watch for weakness in: <strong>{', '.join(bearish_list[:4])}</strong>." if bearish_list else "No major bearish signals right now.")
    )

    # ── Build VIX proxy ───────────────────────────────────────────────────────
    # We don't have direct VIX from Alpaca free tier; estimate from SPY intraday range
    spy_high = spy_snap.get("high", 0)
    spy_low  = spy_snap.get("low", 0)
    spy_mid  = spy_snap.get("price", 1)
    vix_est  = round((spy_high - spy_low) / spy_mid * 100 * 16, 1) if spy_mid else 18.0

    # ── Compile scan payload ──────────────────────────────────────────────────
    scan_payload = {
        "scan_time":  scan_time,
        "scan_time_et": scan_time_et,
        "generated":  scan_time,
        "regime":     regime,
        "summary":    summary,
        "vix_est":    vix_est,
        "spy":        {"price": spy_snap.get("price", 0), "chg": spy_snap.get("chg_pct", 0)},
        "qqq":        {"price": qqq_snap.get("price", 0), "chg": qqq_snap.get("chg_pct", 0)},
        "tickers":    tickers_data,
        "uw_summary": {
            "total_alerts": len(all_uw),
            "bullish":      sum(1 for a in uw_flow if a.get("side") == "bullish"),
            "bearish":      sum(1 for a in uw_flow if a.get("side") == "bearish"),
        },
    }

    # ── Auto-generate plays ───────────────────────────────────────────────────
    plays_list   = generate_plays(tickers_data, uw_flow, scan_time)
    plays_payload = {
        "generated": scan_time,
        "plays":     plays_list,
    }

    # ── News feed ─────────────────────────────────────────────────────────────
    news_payload = build_news_feed(tickers_data, all_uw, scan_time)

    # ── UW whales payload ─────────────────────────────────────────────────────
    whales_payload = {
        "generated": scan_time,
        "items":     all_uw[:200],
        "count":     len(all_uw),
    }

    # ── Write to Supabase ─────────────────────────────────────────────────────
    print("\n  Writing to Supabase...")
    sb_ok = all([
        supabase_upsert("scan",   scan_payload),
        supabase_upsert("plays",  plays_payload),
        supabase_upsert("news",   news_payload),
        supabase_upsert("whales", whales_payload),
    ])

    # ── Write local JSON files (GitHub commit fallback) ───────────────────────
    print("\n  Writing local JSON files (GitHub fallback)...")
    save_json_fallback("scan",   scan_payload)
    save_json_fallback("plays",  plays_payload)
    save_json_fallback("news",   news_payload)
    save_json_fallback("whales", whales_payload)

    print()
    print("=" * 60)
    print(f"  ✅ Scan complete — {scan_time_et}")
    print(f"  Supabase: {'✅ all keys written' if sb_ok else '⚠ partial — check logs'}")
    print(f"  Tickers scored: {len(tickers_data)}")
    print(f"  UW alerts: {len(all_uw)}  (flow={len(uw_flow)}, dp={len(uw_dp)})")
    print(f"  Plays generated: {len(plays_list)}")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
