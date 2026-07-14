#!/usr/bin/env python3
"""
BASANI Market Scanner — run this on your Mac or via cron.
Pulls live data from Alpaca, saves results for grading + report generation.

Saves:
  scan_output.json          — latest scan (Claude reads this)
  history/YYYYMMDD_HHMM.json — archive for grading
"""
import urllib.request, urllib.parse, json, sys, os
from datetime import date, timedelta, datetime

# Env vars take priority; hardcoded values are a local-dev fallback only.
# In GitHub Actions, ALPACA_KEY and ALPACA_SECRET are set as repository secrets.
# Alpaca keys — MUST be set as env vars. No hardcoded fallback for safety.
K = os.environ.get("ALPACA_KEY", "")
S = os.environ.get("ALPACA_SECRET", "")
if not K or not S:
    print("  ❌ ALPACA_KEY / ALPACA_SECRET not set in environment")
    print("     Add to GitHub Actions secrets OR local environment")
    # No scan_output.json write here — OUTPUT_FILE not defined yet
    # run_all.py handles missing scan_output.json gracefully
    sys.exit(0)  # exit cleanly so news/gex still run
H = {"APCA-API-KEY-ID": K, "APCA-API-SECRET-KEY": S}
TICKERS = [
    # Core large caps + market ETFs
    "SPY","QQQ","AAPL","NVDA","AMD","MSFT","AMZN","GOOGL","META","TSLA",
    # High-momentum AI/tech
    "MU","PLTR","COIN","ARM","CRM","NOW","SMCI",
    # Broader market + sectors
    "NFLX","UBER","SHOP","MSTR",
    # Macro / sector ETFs
    "XLF","XLE","XLK","GLD","TLT","SOXS"
]

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(BASE_DIR, "scan_output.json")
HISTORY_DIR = os.path.join(BASE_DIR, "history")
os.makedirs(HISTORY_DIR, exist_ok=True)

def get(path, params={}, _attempt=0):
    """Fetch from Alpaca. Returns None on failure (never raises, never recurses infinitely)."""
    import time
    url = "https://data.alpaca.markets" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=H)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  ❌ HTTP {e.code}: {body[:200]}")
        if e.code == 401:
            print("  → Auth failed — check ALPACA_KEY / ALPACA_SECRET env vars")
            return None          # do not retry on auth failure
        if e.code == 429 and _attempt < 3:
            delay = 30 * (2 ** _attempt)   # 30s, 60s, 120s — capped at 3 attempts
            print(f"  → Rate limited, waiting {delay}s (attempt {_attempt+1}/3)")
            time.sleep(delay)
            return get(path, params, _attempt + 1)
        print(f"  → HTTP {e.code} unrecoverable, skipping")
        return None
    except Exception as e:
        print(f"  ❌ Network error: {e}")
        return None              # let caller decide — never kill the process

def sma(c, n): return round(sum(c[-n:]) / n, 2) if len(c) >= n else None
def ema(c, n):
    k = 2 / (n + 1); e = c[0]
    for p in c[1:]: e = p * k + e * (1 - k)
    return round(e, 2)
def rsi(c, n=14):
    if len(c) < n + 1: return None
    g = [max(c[i]-c[i-1], 0) for i in range(1, len(c))]
    l = [max(c[i-1]-c[i], 0) for i in range(1, len(c))]
    ag = sum(g[-n:]) / n; al = sum(l[-n:]) / n
    return 100 if al == 0 else round(100 - (100 / (1 + ag / al)), 1)

def get_direction(score, chg):
    if score >= 70: return "BULLISH"
    if score <= 30: return "BEARISH"
    return "NEUTRAL"

def target_stop(price, s20, s50, direction):
    if direction == "BULLISH":
        stop = round(s50 * 0.97 if s50 else price * 0.95, 2)
        cons = round(price * 1.05, 2)
        agg  = round(price * 1.12, 2)
    else:
        stop = round(price * 1.05, 2)
        cons = round(price * 0.95, 2)
        agg  = round(price * 0.88, 2)
    return stop, cons, agg

now = datetime.now()
scan_label = now.strftime("%Y%m%d_%H%M")
scan_time  = now.strftime("%Y-%m-%d %H:%M:%S")

print("=" * 65)
print(f"  BASANI SCAN  —  {scan_time}")
print("=" * 65)
print("  Fetching live data from Alpaca...")

start = (date.today() - timedelta(days=90)).isoformat()

bars_resp = get("/v2/stocks/bars", {
    "symbols": ",".join(TICKERS), "timeframe": "1Day",
    "start": start, "limit": 2000
})
if bars_resp is None:
    print("  ❌ Failed to fetch bars from Alpaca — writing empty scan and continuing")
    bars = {}
    snaps = {}
bars = bars_resp.get("bars", {})

snaps_resp = get("/v2/stocks/snapshots", {"symbols": ",".join(TICKERS), "feed": "iex"})
snaps = snaps_resp if snaps_resp is not None else {}

results = []
for t in TICKERS:
    b = bars.get(t, []); snap = snaps.get(t, {})
    if not b: continue
    c     = [x["c"] for x in b]
    price = snap.get("latestTrade", {}).get("p") or c[-1]
    prev  = snap.get("prevDailyBar", {}).get("c") or (c[-2] if len(c) > 1 else price)
    chg   = round((price - prev) / prev * 100, 2)
    hi    = snap.get("dailyBar", {}).get("h", price)
    lo    = snap.get("dailyBar", {}).get("l", price)
    vol   = snap.get("dailyBar", {}).get("v", 0)
    pvol  = snap.get("prevDailyBar", {}).get("v", 1)
    s20   = sma(c, 20); s50 = sma(c, 50); r = rsi(c)
    e8    = ema(c[-20:], 8)  if len(c) >= 20 else None
    e21   = ema(c[-30:], 21) if len(c) >= 30 else None
    score = 0; sig = []
    if s20 and price > s20: score += 20; sig.append("SMA20")
    if s50 and price > s50: score += 20; sig.append("SMA50")
    if e8 and e21 and e8 > e21: score += 20; sig.append("EMA_STACK")
    if r and 45 < r < 75:  score += 20; sig.append(f"RSI_{r}")
    if r and r >= 75:       score += 10; sig.append(f"RSI_HOT_{r}")
    if chg > 2.0:           score += 20; sig.append(f"MOM+{chg}%")
    elif chg > 1.0:         score += 10; sig.append(f"MOM+{chg}%")
    elif chg < -2.0:        score -= 15; sig.append(f"DOWN{chg}%")
    elif chg < -1.0:        score -= 7;  sig.append(f"DOWN{chg}%")
    vol_ratio = vol / pvol if pvol and pvol > 0 else 1
    if vol_ratio > 3.0: score += 15; sig.append(f"VOL_SPIKE_{vol_ratio:.1f}x")
    elif vol_ratio > 1.5: score += 7; sig.append(f"VOL_{vol_ratio:.1f}x")
    # Liquidity filter: penalize low-volume stocks
    if vol < 500000: score -= 10; sig.append("LOW_LIQ")
    score = max(0, min(score, 100))
    direction = get_direction(score, chg)
    stop, cons_tgt, agg_tgt = target_stop(price, s20, s50, direction)
    results.append({
        "ticker": t, "price": round(price, 2), "prev_close": round(prev, 2),
        "chg_pct": chg, "high": round(hi, 2), "low": round(lo, 2), "volume": vol,
        "score": score, "rsi": r, "sma20": s20, "sma50": s50,
        "direction": direction, "signals": sig,
        "stop": stop, "cons_target": cons_tgt, "agg_target": agg_tgt
    })

results.sort(key=lambda x: x["score"], reverse=True)

output = {"scan_time": scan_time, "scan_label": scan_label, "tickers": results}

# Save latest
with open(OUTPUT_FILE, "w") as f:
    json.dump(output, f, indent=2)

# Save to history
hist_file = os.path.join(HISTORY_DIR, f"{scan_label}.json")
with open(hist_file, "w") as f:
    json.dump(output, f, indent=2)

print(f"  ✅ Saved scan_output.json + history/{scan_label}.json\n")
print(f"{'TICKER':<7}{'PRICE':>9}{'CHG%':>8}{'SCORE':>7}{'RSI':>6}{'DIR':>9}  SIGNALS")
print("-" * 70)
for r in results:
    rv = str(r['rsi']) if r['rsi'] else "N/A"
    print(f"{r['ticker']:<7}${r['price']:>8.2f}{r['chg_pct']:>+7.2f}%{r['score']:>7}{rv:>6}{r['direction']:>9}  {' '.join(r['signals'][:3])}")
print("=" * 65)
print("  Done.")

# NOTE: GitHub push and news aggregation are handled by GitHub Actions (scanner.py)
# — do NOT import push or news_feed here.
# Importing news_feed would run the full news pipeline as a side effect.
