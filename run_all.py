#!/usr/bin/env python3
"""
BASANI GitHub Actions Runner
Decides which scripts to run based on current ET time.
Called by the GitHub Actions workflow every 5 minutes.
"""
import subprocess, os, sys
from datetime import datetime, timezone, timedelta

def et_now():
    utc = datetime.now(timezone.utc)
    # Approximate EDT (UTC-4) Apr-Oct, EST (UTC-5) Nov-Mar
    offset = -4 if 3 <= utc.month <= 10 else -5
    return utc + timedelta(hours=offset), utc

def run(script, label=None):
    label = label or script
    print(f"\n{'='*52}")
    print(f"  RUNNING: {label}")
    print(f"{'='*52}")
    r = subprocess.run(["python3", script], capture_output=False)
    ok = r.returncode == 0
    print(f"  {'OK' if ok else 'FAILED'}: {label}")
    return ok

et, utc = et_now()
h   = et.hour
m   = et.minute
wd  = et.weekday()      # 0=Mon 6=Sun
day = et.strftime("%Y-%m-%d %H:%M ET")

is_weekday     = wd < 5
is_premarket   = is_weekday and (8 <= h < 9 or (h == 9 and m < 30))
is_market_open = is_weekday and (h > 9 or (h == 9 and m >= 30)) and h < 16
is_power_hour  = is_weekday and h == 15
is_close_time  = is_weekday and h == 15 and m >= 45

print(f"\nBASSANI Runner @ {day}")
print(f"  market_open={is_market_open}  premarket={is_premarket}  weekday={is_weekday}")

# ── News feed — always (every run) ──────────────────────────────────────────
run("news_feed.py", "News Feed (X + Benzinga + RSS)")

# ── Massive — raw market data: prices, candles, options chains ───────────────
if is_market_open or is_premarket:
    run("massive_feed.py", "Massive Market Data (prices + candles + options chains)")

# ── Scan — during market hours and at pre-market ────────────────────────────
if is_market_open or is_premarket:
    run("scan.py", "Market Scan (Alpaca)")

# ── Unusual Whales — during market hours ────────────────────────────────────
if is_market_open:
    run("uw_client.py", "Unusual Whales (options flow + dark pool)")

# ── Calendar — pre-market and off-hours ─────────────────────────────────────
if is_premarket or not is_weekday or h < 8:
    run("calendar_feed.py", "Calendar Feed (earnings + economic events)")

# ── Daily Report — at close ──────────────────────────────────────────────────
if is_close_time:
    run("daily_report.py", "Daily Report")
