#!/usr/bin/env python3
"""
BASANI Scanner Runner v4 - UW-only

Every data feed is sourced from Unusual Whales (UW). Alpaca, Massive/Polygon,
X/Twitter, and Benzinga have been removed (all returned 401).

Steps run per cycle, gated by ET market time:

  During market hours:
    - uw_client.py      flow + darkpool + congress (every 5 min)
    - scan_v2.py        technical score + conviction overlay + write scan_output.json
    - uw_conviction.py  5-dim conviction per ticker (cached 4h)
    - gex_poller.py     GEX profile for 17-ticker watchlist (every 5 min during hours)

  Pre/post market + weekends:
    - news_feed.py      UW news + Stocktwits + RSS + Forex Factory calendar
    - calendar_feed.py  earnings + economic events

The order is important: news and calendar run fast (<2s) while scan_v2 takes
~3-5s to pull OHLC for 27 tickers. Concurrent where possible, sequential
where tokens are scarce.
"""

import subprocess
import os
import sys
import time
from datetime import datetime, timezone, timedelta


def now_et():
    """Return (ET_now, UTC_now) for scheduling decisions."""
    utc = datetime.now(timezone.utc)
    # Approximate EDT (UTC-4) Apr-Oct, EST (UTC-5) Nov-Mar
    offset = -4 if 4 <= utc.month <= 10 else -5
    return utc + timedelta(hours=offset), utc


def run(label, script, timeout=240):
    """Run a Python script in this directory. Return (ok, elapsed_s)."""
    print()
    print("=" * 60)
    print(f"  RUNNING: {label}")
    print("=" * 60)
    t0 = time.time()
    try:
        r = subprocess.run(
            ["python3", script],
            capture_output=False,
            timeout=timeout,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        ok = r.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT: {label} after {timeout}s")
        ok = False
    except Exception as e:
        print(f"  ERROR: {e}")
        ok = False
    elapsed = int(time.time() - t0)
    print(f"  {'OK' if ok else 'FAIL'}: {label}  ({elapsed}s)")
    return ok, elapsed


def main():
    et, utc = now_et()
    h = et.hour
    m = et.minute
    wd = et.weekday()  # 0=Mon 6=Sun
    is_weekday = wd < 5
    is_premarket = is_weekday and (8 <= h < 9 or (h == 9 and m < 30))
    is_market_open = (
        is_weekday
        and (h > 9 or (h == 9 and m >= 30))
        and h < 16
    )
    is_close_time = is_weekday and h == 15 and m >= 45

    label = et.strftime("%Y-%m-%d %H:%M ET")
    print()
    print("=" * 60)
    print(f"  BASANI Scanner v4  --  {label}")
    print(f"  market_open={is_market_open}  premarket={is_premarket}  weekday={is_weekday}")
    print("=" * 60)

    results = []

    # Always run news - it's fast and cheap
    results.append(run("News Feed (UW + Stocktwits + RSS + Forex)", "news_feed.py", timeout=120))

    # Market-hours only: UW flow + scanner + GEX + conviction
    if is_market_open:
        # Run UW flow + dark pool + congress first (gives dashboard feed)
        results.append(run("Unusual Whales (flow + dark pool + congress)", "uw_client.py", timeout=90))
        # Conviction layer (cached, fast on cache hits)
        results.append(run("UW Conviction (per-ticker 5-dim overlay)", "uw_conviction.py", timeout=120))
        # Scanner pulls OHLC + writes scan_output.json
        results.append(run("Market Scan (UW OHLC + conviction)", "scan_v2.py", timeout=240))
        # Build data.json from scan_output.json (regime, SPY/QQQ summary)
        results.append(run("data.json Aggregator", "data_aggregator.py", timeout=60))
        # GEX poller for the 17-ticker watchlist
        results.append(run("GEX / Gamma Profile", "gex_poller.py", timeout=180))
    elif is_premarket:
        # Pre-market: refresh GEX + conviction once before open
        results.append(run("UW Conviction (premarket refresh)", "uw_conviction.py", timeout=120))
        results.append(run("GEX / Gamma Profile (premarket)", "gex_poller.py", timeout=180))

    # Calendar - pre-market and off-hours
    if is_premarket or not is_weekday or h < 8:
        results.append(run("Calendar (earnings + economic)", "calendar_feed.py", timeout=90))

    # Daily report at close
    if is_close_time:
        results.append(run("Daily Report", "daily_report.py", timeout=120))

    print()
    print("=" * 60)
    print("  SUMMARY")
    print("=" * 60)
    ok_count = sum(1 for ok, _ in results if ok)
    fail_count = len(results) - ok_count
    print(f"  Passed:  {ok_count}")
    print(f"  Failed:  {fail_count}")
    for name, (ok, elapsed) in zip(
        ["news_feed", "uw_client", "uw_conviction", "scan_v2", "gex_poller",
         "calendar_feed", "daily_report"],
        results + [(True, 0)] * (7 - len(results)),
    ):
        if (ok, elapsed) == (True, 0):
            break
        print(f"  {'OK  ' if ok else 'FAIL'} {name:<18} {elapsed}s")
    print("=" * 60)


if __name__ == "__main__":
    main()
