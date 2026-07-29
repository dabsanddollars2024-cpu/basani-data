#!/usr/bin/env python3
"""
BASANI Scanner v2 — Phase 2 upgraded

Replaces scan.py's Alpaca-only data with a hybrid:
  - UW OHLC for candle data (Alpaca was dead since key revocation)
  - UW conviction overlay on top of technical score

Two-layer scoring:
  Layer 1 (Technical, 0-100):
    Same as scan.py: SMA20, SMA50, EMA stack, RSI, momentum, volume

  Layer 2 (Conviction, modifier -50 to +50):
    +25 FLOW_BULL_5M+   (≥$5M net call premium)
    +12 FLOW_BULL        (≥$1M)
    -25 FLOW_BEAR_5M+   (≥$5M net put premium)
    -12 FLOW_BEAR        (≥$1M)
    +15 MKT_RISK_ON      (market-wide bullish)
    -15 MKT_RISK_OFF     (market-wide bearish)
    +12 DP_BUYERS        (≥65% NBBO-positive prints)
    -12 DP_SELLERS
    +8  DP_INSTITUTIONAL (≥2 large $500K+ prints)
    +10 INSIDER_BUY
    -8  INSIDER_SELL     (only if strongly negative)
    +10 GEX_BOUNCE       (price ≤ put_wall + 3%)
    +10 GEX_REJECT       (price ≥ call_wall * 0.97)
    -8  IV_EXPENSIVE
    +5  IV_CHEAP         (boost, since options are cheap to buy)

Final score = clamp(0, 100, technical_score + conviction_modifier)

Direction threshold:
  - technical + conviction agrees → strong bull/bear
  - technical bull but conviction bear → weaken (cap at 65)
  - vice versa: technical bear but conviction bull → reverse signal cap at 40

Output: scan_output_v2.json — same shape as scan_output.json
        PLUS conviction fields, ready for daily_report_v2.py

Runs without Alpaca entirely. UW token required.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
# Import from sibling modules
sys.path.insert(0, str(BASE_DIR))
from uw_conviction import (
    fetch_ticker_conviction, fetch_market_tide, fetch_market_insider,
)

OUTPUT_FILE   = BASE_DIR / "scan_output_v2.json"
TICKERS = [
    # Core large caps + market ETFs
    "SPY","QQQ","AAPL","NVDA","AMD","MSFT","AMZN","GOOGL","META","TSLA",
    # High-momentum AI/tech
    "MU","PLTR","COIN","ARM","CRM","NOW","SMCI",
    # Broader market + sectors
    "NFLX","UBER","SHOP","MSTR",
    # Macro / sector ETFs
    "XLF","XLE","XLK","GLD","TLT","SOXS",
]


# ── UW OHLC for candle data (Alpaca replacement) ─────────────────────────────
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


def fetch_current_price(ticker):
    """
    Get latest 1-minute OHLC bar — returns (price, volume_today).
    Price = close of most recent 1m bar.
    """
    req = urllib.request.Request(
        f"https://api.unusualwhales.com/api/stock/{ticker}/ohlc/1m?limit=5",
        headers={
            "Authorization": "Bearer " + _token(),
            "Accept":        "application/json",
            "User-Agent":    "BASANI-scan-v2/1.0",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read().decode())
        bars = body.get("data", [])
        if not bars:
            return None, None
        latest = bars[0]
        price = float(latest.get("close", 0) or 0)
        # total_volume is cumulative intraday volume
        vol = float(latest.get("total_volume", 0) or 0)
        return price, vol
    except Exception as e:
        print(f"  ❌ {ticker} price fetch: {e}")
        return None, None


def fetch_daily_bars(ticker, days=120):
    """Fetch 1-day OHLC bars for technical indicator calc."""
    req = urllib.request.Request(
        f"https://api.unusualwhales.com/api/stock/{ticker}/ohlc/1d?limit={days}",
        headers={
            "Authorization": "Bearer " + _token(),
            "Accept":        "application/json",
            "User-Agent":    "BASANI-scan-v2/1.0",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode())
        return body.get("data", [])
    except urllib.error.HTTPError as e:
        print(f"  ❌ {ticker} daily bars {e.code}")
        return []
    except Exception as e:
        print(f"  ❌ {ticker} daily bars ERR: {e}")
        return []


def fetch_snapshots(tickers):
    """Batch snapshot fetch (price + volume). Implemented as parallel 1m calls."""
    out = {}
    for t in tickers:
        price, vol = fetch_current_price(t)
        if price is not None:
            out[t] = {"price": price, "volume": vol}
        time.sleep(0.15)
    return out


# ── Technical indicator helpers (same as scan.py) ─────────────────────────────
def sma(c, n):
    return round(sum(c[-n:]) / n, 2) if len(c) >= n else None


def ema(c, n):
    k = 2 / (n + 1)
    e = c[0]
    for p in c[1:]:
        e = p * k + e * (1 - k)
    return round(e, 2)


def rsi(c, n=14):
    if len(c) < n + 1:
        return None
    g = [max(c[i] - c[i-1], 0) for i in range(1, len(c))]
    l = [max(c[i-1] - c[i], 0) for i in range(1, len(c))]
    ag = sum(g[-n:]) / n
    al = sum(l[-n:]) / n
    return 100 if al == 0 else round(100 - (100 / (1 + ag / al)), 1)


# ── Technical scoring (same as scan.py for comparability) ─────────────────────
def technical_score(price, closes, chg, volume, prev_volume):
    score = 0
    sig = []
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    r   = rsi(closes)
    e8  = ema(closes[-20:], 8)  if len(closes) >= 20 else None
    e21 = ema(closes[-30:], 21) if len(closes) >= 30 else None

    if s20 and price > s20: score += 20; sig.append("SMA20")
    if s50 and price > s50: score += 20; sig.append("SMA50")
    if e8 and e21 and e8 > e21: score += 20; sig.append("EMA_STACK")
    if r and 45 < r < 75:    score += 20; sig.append(f"RSI_{r}")
    elif r and r >= 75:       score += 10; sig.append(f"RSI_HOT_{r}")
    if chg > 2.0:             score += 20; sig.append(f"MOM+{chg}%")
    elif chg > 1.0:           score += 10; sig.append(f"MOM+{chg}%")
    elif chg < -2.0:          score -= 15; sig.append(f"DOWN{chg}%")
    elif chg < -1.0:          score -= 7;  sig.append(f"DOWN{chg}%")

    vol_ratio = volume / prev_volume if prev_volume and prev_volume > 0 else 1
    if vol_ratio > 3.0:        score += 15; sig.append(f"VOL_SPIKE_{vol_ratio:.1f}x")
    elif vol_ratio > 1.5:      score += 7;  sig.append(f"VOL_{vol_ratio:.1f}x")
    if volume < 500_000:       score -= 10; sig.append("LOW_LIQ")

    return max(0, min(score, 100)), {
        "sma20": s20, "sma50": s50, "rsi": r,
        "ema8": e8, "ema21": e21,
        "volume_ratio": round(vol_ratio, 2),
    }, sig


# ── Conviction modifier: -50 to +50 ───────────────────────────────────────────
def conviction_modifier(conv):
    """
    Apply UW conviction signals as a score modifier on top of technicals.
    Returns (modifier_int, signal_tags).
    """
    mod = 0
    tags = []

    sigset = set(conv.get("signals", []))

    if "FLOW_BULL_5M+" in sigset:
        mod += 25; tags.append("flow_bull_5m+")
    elif "FLOW_BULL" in sigset:
        mod += 12; tags.append("flow_bull")

    if "FLOW_BEAR_5M+" in sigset:
        mod -= 25; tags.append("flow_bear_5m+")
    elif "FLOW_BEAR" in sigset:
        mod -= 12; tags.append("flow_bear")

    if "MKT_RISK_ON" in sigset:
        mod += 15; tags.append("mkt_risk_on")
    elif "MKT_RISK_OFF" in sigset:
        mod -= 15; tags.append("mkt_risk_off")

    if "DP_BUYERS" in sigset:
        mod += 12; tags.append("dp_buyers")
    elif "DP_SELLERS" in sigset:
        mod -= 12; tags.append("dp_sellers")

    if "DP_INSTITUTIONAL" in sigset:
        mod += 8; tags.append("dp_institutional")

    if "INSIDER_BUY" in sigset:
        mod += 10; tags.append("insider_buy")
    elif "INSIDER_SELL" in sigset:
        mod -= 8; tags.append("insider_sell")

    # IV regime
    iv = conv.get("iv_rank")
    if iv is not None:
        if iv >= 80:
            mod -= 8; tags.append(f"iv_expensive_{iv:.0f}")
        elif iv < 25:
            mod += 5; tags.append(f"iv_cheap_{iv:.0f}")

    # GEX levels — proximity to dealer hedging zone
    price = conv.get("price")  # set by caller
    gamma_flip = conv.get("gex_gamma_flip")
    call_wall   = conv.get("gex_call_wall")
    put_wall    = conv.get("gex_put_wall")

    if price and gamma_flip and call_wall and put_wall:
        # If price is INSIDE [put_wall, call_wall] within 3% of either = supportive zone
        range_size = call_wall - put_wall
        if range_size > 0:
            pos_in_range = (price - put_wall) / range_size
            # Mid-range = neutral, edges = supportive/near magnet
            if 0.4 <= pos_in_range <= 0.6:
                mod += 5; tags.append("gex_midrange")
            elif pos_in_range < 0.15 or pos_in_range > 0.85:
                mod -= 5; tags.append("gex_at_edge")

    return max(-50, min(50, mod)), tags


def combined_direction(tech_score, conv_mod):
    """Final direction flag with conviction-aware threshold."""
    final = tech_score + conv_mod
    if final >= 75:
        return "BULLISH", final
    if final <= 25:
        return "BEARISH", final
    if final >= 60:
        return "BULLISH_WEAK", final
    if final <= 40:
        return "BEARISH_WEAK", final
    return "NEUTRAL", final


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'=' * 75}")
    print(f"  BASANI Scanner v2 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Tickers: {len(TICKERS)} | Data: UW OHLC + UW Conviction")
    print(f"{'=' * 75}\n")

    # 1. Fetch market context (used for all tickers)
    market     = fetch_market_tide()
    mkt_insider = fetch_market_insider()
    if market:
        net = market["cum_net_premium"]
        direction = "RISK_ON" if net > 50_000_000 else ("RISK_OFF" if net < -50_000_000 else "NEUTRAL")
        print(f"  Market tide today: net ${net/1_000_000:+.1f}M → {direction}")
    if mkt_insider:
        print(f"  Insider mkts: buy ${mkt_insider['buy_notional']/1_000_000:+.2f}M / sell ${mkt_insider['sell_notional']/1_000_000:+.2f}M")

    print(f"\n  Pulling conviction layer for all tickers...")

    # 2. Fetch per-ticker conviction (cached per ticker for 4h)
    all_conv = {}
    for t in TICKERS:
        # Need price for GEX zone calc — fetch from info endpoint
        info_url = f"https://api.unusualwhales.com/api/stock/{t}/info"
        try:
            req = urllib.request.Request(info_url, headers={
                "Authorization": "Bearer " + _token(),
                "Accept":        "application/json",
                "User-Agent":    "BASANI-scan-v2/1.0",
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                info = json.loads(r.read().decode()).get("data") or {}
                price = float(info.get("price") or info.get("last_trade_price") or 0) or None
        except Exception:
            price = None

        c = fetch_ticker_conviction(t, market=market)
        c["price"] = price
        all_conv[t] = c
        time.sleep(0.15)
        print(f"    {t:5} pulled", end="\r")
    print(" " * 80, end="\r")
    print(f"  Conviction loaded for {len(all_conv)} tickers\n")

    # 3. Fetch OHLC bars for each ticker
    print(f"  Pulling current prices + daily bars (UW, Alpaca replaced)...")
    all_bars = {}
    snapshots = fetch_snapshots(TICKERS)
    print(f"  Got current prices for {len(snapshots)} tickers")

    for t in TICKERS:
        bars = fetch_daily_bars(t, days=120)
        all_bars[t] = bars
        time.sleep(0.15)
        print(f"    {t:5} bars: {len(bars) if isinstance(bars, list) else 0}", end="\r")
    print(" " * 80, end="\r")
    print(f"  OHLC loaded for {len(all_bars)} tickers\n")

    # 4. Compute final scored rows
    print(f"  Computing technical + conviction scores...")
    results = []
    for t in TICKERS:
        bars = all_bars.get(t, [])
        # Handle both wrapped and bare list responses
        if isinstance(bars, dict):
            bars = bars.get("data", [])
        if not isinstance(bars, list) or not bars:
            print(f"    {t:5} no bars, skip")
            continue

        # Sort by date ascending
        try:
            bars = sorted(bars, key=lambda b: b.get("date") or b.get("time") or "")
        except Exception:
            pass

        # Extract closing prices
        closes = []
        vols   = []
        for b in bars:
            try:
                closes.append(float(b.get("close") or b.get("c") or 0))
                vols.append(float(b.get("volume") or b.get("v") or 0))
            except Exception:
                continue
        closes = [c for c in closes if c > 0]
        if len(closes) < 30:
            print(f"    {t:5} insufficient history ({len(closes)} days), skip")
            continue

        price    = closes[-1]
        prev     = closes[-2] if len(closes) > 1 else price
        # Use live snapshot price if available (1m candle = current intraday)
        snap = snapshots.get(t, {})
        if snap.get("price"):
            price = snap["price"]
        chg      = round((price - prev) / prev * 100, 2) if prev else 0
        vol      = snap.get("volume") or (vols[-1] if vols else 0)
        prev_vol = vols[-2] if len(vols) > 1 else vol

        tech, indicators, tech_sigs = technical_score(price, closes, chg, vol, prev_vol)
        conv = all_conv.get(t, {})

        # Inject price into conviction for GEX zone calc
        conv_mod, conv_sigs = conviction_modifier(conv)
        final_dir, final_score = combined_direction(tech, conv_mod)

        # Build result row
        results.append({
            "ticker":              t,
            "price":               round(price, 2),
            "prev_close":          round(prev, 2),
            "chg_pct":             chg,
            "volume":              int(vol),
            "score":               round(final_score, 0),  # combined score
            "technical_score":     round(tech, 0),
            "conviction_modifier": round(conv_mod, 0),
            "direction":           final_dir,
            "signals":             tech_sigs + conv_sigs,  # all tags visible
            "rsi":                 indicators["rsi"],
            "sma20":               indicators["sma20"],
            "sma50":               indicators["sma50"],
            "iv_rank":             conv.get("iv_rank"),
            "gex_call_wall":       conv.get("gex_call_wall"),
            "gex_put_wall":        conv.get("gex_put_wall"),
            "gex_gamma_flip":      conv.get("gex_gamma_flip"),
            "gex_gamma_magnet":    conv.get("gex_gamma_magnet"),
            "flow_net_premium":    conv.get("flow_net_premium"),
            "dp_total_premium":    conv.get("dp_total_premium"),
            "dp_nbbo_ratio":       conv.get("dp_nbbo_ratio"),
            "dp_large_prints":     conv.get("dp_large_prints"),
            "insider_net_premium": conv.get("insider_net_premium"),
            "bar_count":           len(closes),
        })

    # Sort by combined score descending
    results.sort(key=lambda x: x["score"], reverse=True)

    # 5. Save output (atomic write)
    output = {
        "scan_time":  datetime.now().isoformat(),
        "scan_label": datetime.now().strftime("%Y%m%d_%H%M"),
        "market":     market,
        "source":     "unusual_whales (OHLC + conviction)",
        "tickers":    results,
    }
    tmp_path = OUTPUT_FILE.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(output, indent=2, default=str))
    os.replace(tmp_path, OUTPUT_FILE)

    # Also write to the canonical dashboard filename "scan_output.json"
    # so the live dashboard at dabsanddollars2024-cpu/basani-data fetches fresh data.
    # Atomic: write to .tmp, then rename, so concurrent reads never see half-written JSON.
    canonical = BASE_DIR / "scan_output.json"
    canonical_tmp = canonical.with_suffix(".tmp")
    canonical_tmp.write_text(json.dumps(output, indent=2, default=str))
    os.replace(canonical_tmp, canonical)

    # 6. Print summary
    bull_count = sum(1 for r in results if r["direction"] in ("BULLISH", "BULLISH_WEAK"))
    bear_count = sum(1 for r in results if r["direction"] in ("BEARISH", "BEARISH_WEAK"))

    print(f"\n{'=' * 75}")
    print(f"  RESULTS — {len(results)} tickers scored")
    print(f"{'=' * 75}")
    print(f"  {'TIC':5} {'Pri':>7} {'Chg%':>6} {'Tech':>4} {'Conv':>4} {'Final':>5} {'Dir':14}  Tags")
    print(f"  {'---':5} {'---':>7} {'---':>6} {'---':>4} {'---':>4} {'-----':>5} {'---':14}  ----")
    for r in results:
        chg = r["chg_pct"]
        # show top 3 conviction tags max
        conv_tags = r["signals"][len(r["signals"]) - 0:]  # keep them all, but show first 4
        net_flow_m = (r.get("flow_net_premium") or 0) / 1_000_000
        conv_summary = f"flow{net_flow_m:+.1f}M iv={r.get('iv_rank') or 0:.0f}"
        tech_str = str(r["technical_score"])
        conv_str = f"{r['conviction_modifier']:+d}"
        final_str = str(r["score"])
        print(f"  {r['ticker']:5} ${r['price']:>6.2f} {chg:>+5.2f}% {tech_str:>4} {conv_str:>4} {final_str:>5} {r['direction']:14}  {conv_summary}")

    print(f"\n  Total: {bull_count} bullish, {bear_count} bearish, {len(results) - bull_count - bear_count} neutral")
    print(f"\n  Saved → {OUTPUT_FILE}")
    print(f"{'=' * 75}\n")


if __name__ == "__main__":
    main()
