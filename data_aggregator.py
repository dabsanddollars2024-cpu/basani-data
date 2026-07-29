#!/usr/bin/env python3
"""
BASANI data.json aggregator - writes scan_output.json + data.json + summary.json
from a single UW-driven scan. Reads scan_output.json + UW market tide to build
the dashboard summary blob (regime, SPY/QQQ summary, top signal, weak spots).

Why this exists:
  Before v4 the dashboard read data.json for SPY/QQQ/regime/top-signal display.
  The aggregator that produced it broke silently - hence the dashboard showed
  19-day-stale data. This rebuilds data.json from scan_output.json (which is
  freshly written by scan_v2.py + uw_conviction.py) and a single UW market-tide
  call for the day's bias.

Runs after scan_v2.py. Safe to re-run; output is atomic via .tmp + rename.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(BASE_DIR))
from uw_http import uw_get_json

SCAN_OUTPUT  = BASE_DIR / "scan_output.json"
DATA_OUTPUT  = BASE_DIR / "data.json"


def derive_regime(tickers):
    """Estimate market regime from SPY + QQQ scores."""
    spy = next((t for t in tickers if t["ticker"] == "SPY"), None)
    qqq = next((t for t in tickers if t["ticker"] == "QQQ"), None)
    if not spy and not qqq:
        return "NEUTRAL"
    avg = (spy["score"] + qqq["score"]) / 2 if spy and qqq else (spy or qqq)["score"]
    if avg >= 65:
        return "BULLISH BIAS"
    if avg <= 35:
        return "BEARISH"
    return "NEUTRAL"


def main():
    if not SCAN_OUTPUT.exists():
        print(f"  FAIL: {SCAN_OUTPUT} missing - run scan_v2.py first")
        sys.exit(1)
    with SCAN_OUTPUT.open() as f:
        scan = json.load(f)
    tickers = scan.get("tickers", [])
    if not tickers:
        print("  FAIL: scan_output.json has no tickers")
        sys.exit(1)

    # Market tide from UW (gives today's net call vs put premium)
    tide, code = uw_get_json("/api/market/market-tide")
    net_premium = 0
    if code == 200 and isinstance(tide, dict):
        rows = tide.get("data", [])
        if rows:
            def _f(v):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return 0
            call = _f(rows[-1].get("net_call_premium"))
            put = _f(rows[-1].get("net_put_premium"))
            net_premium = call - put

    spy = next((t for t in tickers if t["ticker"] == "SPY"), {})
    qqq = next((t for t in tickers if t["ticker"] == "QQQ"), {})

    regime = derive_regime(tickers)
    top = tickers[0] if tickers else {}
    bearish_tickers = [t["ticker"] for t in tickers if t.get("direction", "").startswith("BEARISH")]

    summary_text = (
        f"Market is showing a <strong>{regime}</strong> tone as of "
        f"{scan['scan_time']}. Top signal: <strong>{top.get('ticker','?')}</strong> "
        f"(score {top.get('score','?')}, {top.get('chg_pct', 0):+.2f}%, "
        f"{top.get('direction','?')}). "
        + (f"Watch for weakness in: <strong>{', '.join(bearish_tickers[:5])}</strong>."
           if bearish_tickers else "No major bearish signals in the scan right now.")
    )

    out = {
        "scan_time": scan.get("scan_time"),
        "scan_time_et": datetime.now().strftime("%Y-%m-%d %H:%M ET"),
        "generated": datetime.now(timezone.utc).isoformat(),
        "regime": regime,
        "summary": summary_text,
        "net_premium": round(net_premium, 2),
        "spy": {
            "price": spy.get("price"),
            "chg": round(spy.get("chg_pct", 0), 2),
        },
        "qqq": {
            "price": qqq.get("price"),
            "chg": round(qqq.get("chg_pct", 0), 2),
        },
        "tickers": tickers,
        "source": "unusual_whales",
        "scanner": "scan_v2",
    }

    tmp = DATA_OUTPUT.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(out, f, indent=2, default=str)
    os.replace(tmp, DATA_OUTPUT)

    print("=" * 60)
    print(f"  BASANI data.json aggregator  -  {out['scan_time_et']}")
    print("=" * 60)
    print(f"  regime:    {regime}")
    print(f"  SPY:       ${out['spy']['price']} ({out['spy']['chg']:+.2f}%)")
    print(f"  QQQ:       ${out['qqq']['price']} ({out['qqq']['chg']:+.2f}%)")
    print(f"  Top:       {top.get('ticker')} ({top.get('score')}, {top.get('direction')})")
    print(f"  Net prem:  ${net_premium/1_000_000:+.1f}M")
    print(f"  Bearish:   {', '.join(bearish_tickers[:5]) or 'none'}")
    print(f"  Saved    -> {DATA_OUTPUT.name} ({DATA_OUTPUT.stat().st_size//1024} KB)")
    print("=" * 60)


if __name__ == "__main__":
    main()
