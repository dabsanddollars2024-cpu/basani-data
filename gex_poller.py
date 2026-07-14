#!/usr/bin/env python3
"""
BASANI Gamma Exposure (GEX) Poller — pulls GEX, greeks, IV for 17-ticker watchlist
from Unusual Whales API. Runs daily at 9:35 AM ET.

Stores:
  /home/client_4319_1/basani_live/gex_data.json — scanner table data
  /home/client_4319_1/basani_live/gex_detail_<ticker>.json — deep-dive per ticker

Endpoints used (from UW skill.md):
  /api/stock/{ticker}/spot-exposures/strike  — SPOT GEX by strike (GEX profile)
  /api/stock/{ticker}/greek-exposure/strike  — Static GEX (overnight EOD)
  /api/stock/{ticker}/greeks                  — Greeks per strike/expiry
  /api/stock/{ticker}/interpolated-iv         — IV rank + percentile
  /api/stock/{ticker}/options-volume          — Put/call ratio
"""
import json, os, sys, time, gzip
import urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

DIR    = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(DIR, "gex_data.json")

# 17-ticker watchlist (per Dabs spec)
WATCHLIST = [
    "SPY", "QQQ", "NVDA", "TSLA", "META", "AAPL", "MSFT",
    "AMD", "AMZN", "GOOGL", "MU", "PLTR", "COIN", "XLF", "XLE", "GLD", "TLT"
]

# ── UW AUTH (required headers per skill.md) ─────────────────────────────────
def get_token():
    tok = os.environ.get("UNUSUAL_WHALES_API_KEY")
    if not tok:
        # Fall back to file
        tok_path = os.path.join(DIR, "..", ".hermes", ".env")
        try:
            with open(tok_path) as f:
                for line in f:
                    if "UNUSUAL_WHALES_API_KEY" in line and "=" in line:
                        tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except Exception:
            pass
    return tok

def get_headers(tok):
    return {
        "Authorization":      "Bearer " + tok,
        "UW-CLIENT-API-ID":   "100001",     # Required by UW per skill.md
        "Accept":             "application/json",
        "User-Agent":         "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language":    "en-US,en;q=0.9",
        "Origin":             "https://unusualwhales.com",
        "Referer":            "https://unusualwhales.com/",
    }

def uw_get(path, tok, timeout=20):
    url = "https://api.unusualwhales.com" + path
    req = urllib.request.Request(url, headers=get_headers(tok))
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return json.loads(body), r.status
    except urllib.error.HTTPError as e:
        body = e.read()
        if e.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        try:
            return json.loads(body), e.code
        except Exception:
            return {"error": body[:200].decode("utf-8", errors="ignore")}, e.code
    except Exception as e:
        return {"error": str(e)}, 0


# ── AGGREGATE GEX DATA ──────────────────────────────────────────────────────
def fetch_ticker(ticker, tok):
    """Fetch all GEX-related data for one ticker. Returns dict or None on failure."""
    out = {"ticker": ticker, "fetched_at": datetime.now(timezone.utc).isoformat()}

    # 1. SPOT GEX by strike — the main GEX profile chart
    spot, status = uw_get(f"/api/stock/{ticker}/spot-exposures/strike", tok)
    if status == 200:
        out["spot_exposures"] = spot
    else:
        out["spot_exposures_error"] = spot

    # 2. Static GEX by strike (end-of-day)
    static, status = uw_get(f"/api/stock/{ticker}/greek-exposure/strike", tok)
    if status == 200:
        out["static_exposures"] = static
    else:
        out["static_exposures_error"] = static

    # 3. Interpolated IV
    iv, status = uw_get(f"/api/stock/{ticker}/interpolated-iv", tok)
    if status == 200:
        out["iv"] = iv
    else:
        out["iv_error"] = iv

    # 4. Options volume (PC ratio)
    vol, status = uw_get(f"/api/stock/{ticker}/options-volume", tok)
    if status == 200:
        out["options_volume"] = vol
    else:
        out["options_volume_error"] = vol

    # 5. Max pain — strike where option sellers' total loss is minimized
    mp, status = uw_get(f"/api/stock/{ticker}/max-pain", tok)
    if status == 200:
        out["max_pain"] = mp
    else:
        out["max_pain_error"] = mp

    return out


def compute_summary(ticker_data):
    """Compute summary stats from GEX exposures for the scanner table."""
    summary = {
        "ticker":            ticker_data["ticker"],
        "spot_exposures_ok": "spot_exposures" in ticker_data,
        "iv_rank":           None,
        "iv_percentile":     None,
        "net_gex":           None,
        "call_wall":         None,
        "call_wall_size":    None,    # GEX magnitude at call wall ($M)
        "put_wall":          None,
        "put_wall_size":     None,    # abs(GEX) at put wall ($M)
        "max_pain":          None,
        "spot":              None,
        "expected_move_pct": None,
        "signal":            None,
        "fetched_at":        ticker_data.get("fetched_at"),
    }

    # IV rank from interpolated-iv
    iv = ticker_data.get("iv", {}).get("data") or ticker_data.get("iv")
    if isinstance(iv, dict):
        summary["iv_rank"]       = iv.get("iv_rank") or iv.get("rank")
        summary["iv_percentile"] = iv.get("iv_percentile")

    # GEX profile: find call wall (max positive gamma above spot) and put wall (max abs negative gamma below spot)
    exposures = (ticker_data.get("spot_exposures") or {}).get("data") or ticker_data.get("spot_exposures")
    if isinstance(exposures, list) and exposures:
        # Spot price = strike closest to ATM or extracted from first item
        summary["spot"] = exposures[0].get("underlying_price") or exposures[0].get("spot_price")
        if not summary["spot"]:
            # try to find ATM strike
            for e in exposures:
                if e.get("strike"):
                    summary["spot"] = e.get("strike")
                    break

        # Aggregate net GEX per strike
        strikes = {}
        for e in exposures:
            k = e.get("strike")
            if k is None:
                continue
            gex = e.get("gamma_exposure") or e.get("gex") or e.get("exposure") or e.get("call_gamma_exposure", 0) - e.get("put_gamma_exposure", 0)
            strikes.setdefault(k, 0)
            strikes[k] += gex

        # Net GEX = sum
        summary["net_gex"] = sum(strikes.values())

        # Call wall = strike with max GEX above spot
        # Put wall = strike with min (most negative) GEX below spot
        spot = summary["spot"] or 0
        above = {k: v for k, v in strikes.items() if k > spot}
        below = {k: v for k, v in strikes.items() if k < spot}
        if above:
            summary["call_wall"]      = max(above, key=above.get)
            summary["call_wall_size"] = above[summary["call_wall"]]
        if below:
            summary["put_wall"]      = min(below, key=below.get)
            summary["put_wall_size"] = below[summary["put_wall"]]  # negative; abs taken at render

        # Max pain — strike where option buyers lose the most / sellers profit the most
        mp_data = ticker_data.get("max_pain", {})
        if isinstance(mp_data, dict):
            mp_inner = mp_data.get("data") or mp_data
            mp_val = mp_inner.get("max_pain") or mp_inner.get("strike") or mp_inner.get("price")
            if mp_val is not None and isinstance(mp_val, (int, float)) and mp_val > 0:
                summary["max_pain"] = float(mp_val)

        # Expected move %: avg of (call_wall - spot) / spot and (spot - put_wall) / spot
        if summary["call_wall"] and summary["put_wall"] and spot > 0:
            up_move   = (summary["call_wall"] - spot) / spot * 100
            dn_move   = (spot - summary["put_wall"]) / spot * 100
            summary["expected_move_pct"] = round((up_move + dn_move) / 2, 2)

        # ── SIGNAL LOGIC (per Dabs spec) ─────────────────────────────────────
        # PIN ▲: GEX positive + call wall ≤1% above + put wall ≤2% below
        # FLOOR ▼: GEX negative + put wall ≤1.5% below
        # EXPAND ⇕: GEX negative + walls >3% away
        # CHOP ─: GEX flip + walls balanced
        net = summary["net_gex"] or 0
        cw  = summary["call_wall"]
        pw  = summary["put_wall"]
        if cw and pw and spot > 0:
            cw_dist = (cw - spot) / spot * 100
            pw_dist = (spot - pw) / spot * 100
            if net > 0 and cw_dist <= 1.0 and pw_dist <= 2.0:
                summary["signal"] = "PIN"
            elif net < 0 and pw_dist <= 1.5:
                summary["signal"] = "FLOOR"
            elif net < 0 and cw_dist > 3.0 and pw_dist > 3.0:
                summary["signal"] = "EXPAND"
            elif abs(net) < (summary["net_gex"] or 0) * 0.1:  # GEX flip
                summary["signal"] = "CHOP"
            else:
                summary["signal"] = "—"
        else:
            summary["signal"] = "—"

        # ── SUPPORT / RESISTANCE ZONES ────────────────────────────────────────
        # Cluster nearby strikes with same-sign GEX into zones.
        # Result: top resistance zones (where dealers will sell rallies)
        #         and top support zones (where dealers will buy dips).
        summary["resistance_zones"] = _build_zones(strikes, spot, side="above")
        summary["support_zones"]    = _build_zones(strikes, spot, side="below")

    return summary


def _build_zones(strikes, spot, side, proximity_pct=1.5, max_zones=3):
    """
    Cluster strikes within proximity_pct of each other (relative to spot) into zones.
    Returns a list of zone dicts sorted by strength:
      {level: float, gex: float, strikes: [list], strength: 'MAJOR'|'MINOR', distance_pct: float}

    Args:
      strikes:      {strike: gex_value} dict (already aggregated)
      spot:         current price (for distance calculation)
      side:         'above' (resistance) or 'below' (support)
      proximity_pct: strikes within this % of each other cluster together
      max_zones:    return at most this many top zones
    """
    if not strikes or not spot:
        return []

    # Filter to side, sort by strike
    if side == "above":
        side_strikes = {k: v for k, v in strikes.items() if k > spot}
        # Positive GEX = resistance (dealers sell into rallies)
        # Filter to positive values; sort by gex desc within strike distance
    else:
        side_strikes = {k: v for k, v in strikes.items() if k < spot}
        # Negative GEX = support (dealers buy dips), but for magnitude use abs

    if not side_strikes:
        return []

    # Sort by strike distance from spot
    sorted_strikes = sorted(side_strikes.items(), key=lambda x: x[0])

    # Greedy clustering: walk through sorted strikes, start new zone when gap > proximity_pct
    zones = []
    current_zone = None
    proximity_threshold = spot * (proximity_pct / 100.0)

    for strike, gex in sorted_strikes:
        if current_zone is None:
            current_zone = {"strikes": [strike], "gex_vals": [gex], "min_strike": strike, "max_strike": strike}
        else:
            # If this strike is within proximity of the zone's nearest strike, extend
            gap = strike - current_zone["max_strike"]
            if gap <= proximity_threshold:
                current_zone["strikes"].append(strike)
                current_zone["gex_vals"].append(gex)
                current_zone["max_strike"] = strike
            else:
                zones.append(current_zone)
                current_zone = {"strikes": [strike], "gex_vals": [gex], "min_strike": strike, "max_strike": strike}
    if current_zone is not None:
        zones.append(current_zone)

    # Build zone summaries
    out = []
    for z in zones:
        # Zone center = midpoint, weighted by GEX magnitude
        total_abs = sum(abs(v) for v in z["gex_vals"])
        if total_abs == 0:
            continue
        weighted_strike = sum(z["strikes"][i] * abs(z["gex_vals"][i]) for i in range(len(z["strikes"]))) / total_abs
        avg_gex = sum(z["gex_vals"]) / len(z["gex_vals"])
        distance_pct = abs(weighted_strike - spot) / spot * 100

        out.append({
            "level":          round(weighted_strike, 2),
            "strikes":        sorted(z["strikes"]),
            "strike_count":   len(z["strikes"]),
            "gex":            avg_gex,
            "gex_total":      sum(z["gex_vals"]),
            "distance_pct":   round(distance_pct, 2),
        })

    # Sort by absolute GEX total (strongest zones first)
    out.sort(key=lambda z: abs(z["gex_total"]), reverse=True)

    # Tag strength: top 1 = MAJOR, rest = MINOR
    for i, z in enumerate(out):
        z["strength"] = "MAJOR" if i == 0 else "MINOR"
        z["type"] = "RESISTANCE" if side == "above" else "SUPPORT"

    return out[:max_zones]


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print("="*60)
    print(f"  BASANI GEX Poller — {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    print("="*60)

    tok = get_token()
    if not tok:
        print("  FAIL: No UW token in env UNUSUAL_WHALES_API_KEY")
        return False

    print(f"  Token: {tok[:8]}...{tok[-6:]} ({len(tok)} chars)")
    print(f"  Watchlist: {len(WATCHLIST)} tickers")
    print()

    results = []
    summaries = []
    errors = []

    for i, ticker in enumerate(WATCHLIST, 1):
        print(f"  [{i}/{len(WATCHLIST)}] {ticker}...")
        data = fetch_ticker(ticker, tok)
        # Save per-ticker detail file
        detail_path = os.path.join(DIR, f"gex_detail_{ticker}.json")
        with open(detail_path, "w") as f:
            json.dump(data, f, indent=2)
        # Compute summary
        s = compute_summary(data)
        summaries.append(s)
        # Check for errors
        err_keys = [k for k in data if k.endswith("_error")]
        if err_keys:
            for k in err_keys:
                err = data[k]
                code = (err or {}).get("code", "?") if isinstance(err, dict) else "?"
                if code == "daily_request_limit_hit":
                    errors.append(f"{ticker}: QUOTA EXHAUSTED")
                    print(f"    ⛔ Quota exhausted — stopping")
                    return save_results(summaries, errors)
                else:
                    print(f"    ⚠️  {k}: {code}")

        # Small delay to avoid hammering
        time.sleep(0.3)

    return save_results(summaries, errors)


def save_results(summaries, errors):
    output = {
        "generated":      datetime.now(timezone.utc).isoformat(),
        "generated_display": datetime.now().strftime("%Y-%m-%d %H:%M ET"),
        "watchlist":      [s["ticker"] for s in summaries],
        "count":          len(summaries),
        "summaries":      summaries,
        "errors":         errors,
        "source":         "unusual_whales",
        "next_refresh":   "manual (daily 9:35 AM ET recommended)",
    }
    with open(OUTPUT, "w") as f:
        json.dump(output, f, indent=2)
    print()
    print(f"  ✅  Saved {len(summaries)} tickers → gex_data.json")
    if errors:
        print(f"  ⚠️  {len(errors)} errors: {errors}")
    return len(summaries) > 0


if __name__ == "__main__":
    main()
