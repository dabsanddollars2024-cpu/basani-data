#!/usr/bin/env python3
"""
BASANI — Calendar Feed
Fetches forward-facing events from Benzinga + Unusual Whales:
  • Earnings calendar (next 30 days)
  • Economic calendar (next 14 days)
  • FDA / IPO / Conference events (next 14 days)
Writes to calendar.json for the dashboard.
"""

import json, os, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta

BZ_KEY  = os.environ.get("BENZINGA_KEY", "TUUL5FC2AC")
UW_KEY  = os.environ.get("UW_TOKEN",    "")
DIR     = os.path.dirname(os.path.abspath(__file__))
OUTPUT  = os.path.join(DIR, "calendar.json")

now       = datetime.now(timezone.utc)
today_str = now.strftime("%Y-%m-%d")
in14      = (now + timedelta(days=14)).strftime("%Y-%m-%d")
in30      = (now + timedelta(days=30)).strftime("%Y-%m-%d")

# ── HTTP HELPERS ─────────────────────────────────────────────────────────────
def bz_get(path, params):
    params["token"] = BZ_KEY
    url = "https://api.benzinga.com" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept":"application/json","User-Agent":"BASANI/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw)
    except Exception as e:
        print(f"  BZ FAIL {path}: {e}")
        return []

def uw_get(path, params=None):
    url = "https://api.unusualwhales.com" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + UW_KEY,
        "Accept": "application/json", "User-Agent": "BASANI/1.0"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            return data if isinstance(data, list) else data.get("data", [])
    except Exception as e:
        print(f"  UW FAIL {path}: {e}")
        return []

# ── EARNINGS ─────────────────────────────────────────────────────────────────
def fetch_earnings():
    events = []
    raw = bz_get("/api/v2.1/calendar/earnings", {
        "date_from": today_str, "date_to": in30,
        "parameters[importance]": "4,5",
        "pageSize": "200"
    })
    rows = raw if isinstance(raw, list) else raw.get("earnings", [])
    for r in rows:
        ticker = (r.get("ticker") or r.get("name") or "").upper()
        if not ticker: continue
        date   = r.get("date") or r.get("date_ex") or ""
        time   = r.get("time") or "tbd"         # "pre-market" / "after-market" / "during-market"
        eps_est = r.get("eps_est") or r.get("estimate") or ""
        rev_est = r.get("revenue_est") or ""
        name    = r.get("name") or ticker

        events.append({
            "type":     "earnings",
            "date":     date[:10] if date else "",
            "ticker":   ticker,
            "name":     name,
            "time":     time,
            "eps_est":  str(eps_est),
            "rev_est":  str(rev_est),
            "source":   "benzinga",
            "color":    "#1a6bcc"
        })
    print(f"  earnings      → {len(events)} events")
    return events

# ── ECONOMIC CALENDAR ─────────────────────────────────────────────────────────
def fetch_economics():
    events = []
    raw = bz_get("/api/v2.1/calendar/economics", {
        "date_from": today_str, "date_to": in14,
        "parameters[importance]": "3,4,5"
    })
    rows = raw if isinstance(raw, list) else raw.get("economics", [])
    for r in rows:
        name = r.get("event_name") or r.get("name") or r.get("event") or ""
        if not name: continue
        imp  = int(r.get("importance", 3))
        color = "#c0392b" if imp >= 4 else "#e67e22" if imp == 3 else "#888"
        events.append({
            "type":      "economic",
            "date":      (r.get("date") or r.get("event_date") or "")[:10],
            "time":      r.get("time") or r.get("event_time") or "",
            "name":      name,
            "currency":  r.get("country") or r.get("currency") or "USD",
            "forecast":  str(r.get("consensus") or r.get("forecast") or ""),
            "previous":  str(r.get("prior") or r.get("previous") or ""),
            "actual":    str(r.get("actual") or ""),
            "importance": imp,
            "source":    "benzinga",
            "color":     color
        })
    print(f"  economics     → {len(events)} events")
    return events

# ── FDA CALENDAR ──────────────────────────────────────────────────────────────
def fetch_fda():
    events = []
    raw = bz_get("/api/v2.1/calendar/fda", {
        "date_from": today_str, "date_to": in30,
        "pageSize": "50"
    })
    rows = raw if isinstance(raw, list) else raw.get("fda", [])
    for r in rows:
        ticker = (r.get("ticker") or "").upper()
        drug   = r.get("drug_name") or r.get("drug") or ""
        status = r.get("stage") or r.get("status") or ""
        events.append({
            "type":   "fda",
            "date":   (r.get("date") or r.get("event_date") or "")[:10],
            "ticker": ticker,
            "name":   f"FDA: {drug}" if drug else "FDA Decision",
            "status": status,
            "source": "benzinga",
            "color":  "#8e44ad"
        })
    print(f"  FDA           → {len(events)} events")
    return events

# ── IPO CALENDAR ──────────────────────────────────────────────────────────────
def fetch_ipos():
    events = []
    raw = bz_get("/api/v2.1/calendar/ipos", {
        "date_from": today_str, "date_to": in30,
        "pageSize": "50"
    })
    rows = raw if isinstance(raw, list) else raw.get("ipos", [])
    for r in rows:
        ticker = (r.get("ticker") or "").upper()
        name   = r.get("company") or r.get("name") or ticker
        price  = r.get("price") or r.get("price_range") or ""
        events.append({
            "type":   "ipo",
            "date":   (r.get("date") or r.get("pricing_date") or "")[:10],
            "ticker": ticker,
            "name":   f"IPO: {name}",
            "price":  str(price),
            "source": "benzinga",
            "color":  "#16a085"
        })
    print(f"  IPOs          → {len(events)} events")
    return events

# ── UW ECONOMIC CALENDAR ─────────────────────────────────────────────────────
# Keyword-based importance scorer (UW doesn't provide impact ratings)
_HIGH_IMPACT = [
    "employment report", "nonfarm", "unemployment rate", "consumer price",
    "cpi", "inflation", "federal reserve", "fomc", "interest rate", "gdp",
    "gross domestic", "fed chair", "powell"
]
_MED_HIGH_IMPACT = [
    "hourly wages", "adp employment", "ism manufactur", "ism services",
    "trade deficit", "jobless claims", "retail sales", "pce", "core pce",
    "consumer spending", "durable goods"
]
_MED_IMPACT = [
    "consumer sentiment", "consumer confidence", "home sales", "consumer credit",
    "wholesale", "construction", "job openings", "jolts", "ism", "chicago fed",
    "new york fed", "fed president", "fed governor", "williams", "goolsbee",
    "bowman", "waller", "daly"
]

def _uw_importance(event_name: str) -> tuple[int, str]:
    """Return (importance 1-5, hex_color) for a UW economic event."""
    name_lower = event_name.lower()
    for kw in _HIGH_IMPACT:
        if kw in name_lower:
            return 5, "#c0392b"
    for kw in _MED_HIGH_IMPACT:
        if kw in name_lower:
            return 4, "#c0392b"
    for kw in _MED_IMPACT:
        if kw in name_lower:
            return 3, "#e67e22"
    return 2, "#888"


def fetch_uw_economic():
    """Pull macro economic calendar from Unusual Whales /api/market/economic-calendar."""
    events = []
    raw = uw_get("/api/market/economic-calendar")
    rows = raw if isinstance(raw, list) else (raw.get("data") if isinstance(raw, dict) else [])
    for r in rows:
        name = r.get("event") or ""
        if not name:
            continue
        iso_time = r.get("time") or ""
        date_str  = iso_time[:10] if iso_time else ""
        time_str  = iso_time[11:16] if len(iso_time) > 10 else ""
        importance, color = _uw_importance(name)
        events.append({
            "type":       "economic",
            "date":       date_str,
            "time":       time_str,
            "name":       name,
            "currency":   "USD",
            "forecast":   str(r.get("forecast") or ""),
            "previous":   str(r.get("prev") or ""),
            "actual":     "",
            "period":     str(r.get("reported_period") or ""),
            "importance": importance,
            "source":     "unusual_whales",
            "color":      color
        })
    print(f"  UW economic   → {len(events)} events")
    return events


# ── UW EARNINGS (cross-reference) ─────────────────────────────────────────────
def fetch_uw_earnings():
    events = []
    seen_keys = set()
    # Sweep next 14 calendar days — both pre-market and after-hours per day
    for delta in range(0, 15):
        day = (now + timedelta(days=delta)).strftime("%Y-%m-%d")
        for endpoint, when_label in [
            ("/api/earnings/premarket",  "pre-market"),
            ("/api/earnings/afterhours", "after-market"),
        ]:
            rows = uw_get(endpoint, {"date": day})
            for r in rows:
                ticker = (r.get("ticker") or r.get("symbol") or "").upper()
                if not ticker: continue
                key = f"{day}-{ticker}-{when_label}"
                if key in seen_keys: continue
                seen_keys.add(key)
                events.append({
                    "type":    "earnings",
                    "date":    day,
                    "ticker":  ticker,
                    "name":    r.get("name") or r.get("full_name") or ticker,
                    "time":    r.get("when") or when_label,
                    "eps_est": str(r.get("eps_estimate") or r.get("eps_est") or ""),
                    "rev_est": "",
                    "source":  "unusual_whales",
                    "color":   "#1a6bcc"
                })
    print(f"  UW earnings   → {len(events)} events (14-day sweep)")
    return events

# ── MAIN ──────────────────────────────────────────────────────────────────────
# ── BASANI HARDCODED MACRO EVENTS ─────────────────────────────────────────────
# Key events always injected so the calendar never shows blank for critical dates.
# Update this list each quarter. Sorted by date.
BASANI_MACRO_EVENTS = [
    # May 2026
    {"type":"economic","date":"2026-05-06","time":"08:30","name":"FOMC Rate Decision — May 7 (pre-market)",
     "currency":"USD","importance":5,"color":"#c0392b","source":"basani_macro","forecast":"Hold 3.5-3.75%","previous":"3.5-3.75%","actual":""},
    {"type":"economic","date":"2026-05-07","time":"14:00","name":"FOMC Rate Decision + Powell Press Conference",
     "currency":"USD","importance":5,"color":"#c0392b","source":"basani_macro","forecast":"Hold 3.5-3.75%","previous":"3.5-3.75%","actual":""},
    {"type":"economic","date":"2026-05-13","time":"08:30","name":"CPI — April 2026",
     "currency":"USD","importance":5,"color":"#c0392b","source":"basani_macro","forecast":"","previous":"","actual":""},
    {"type":"economic","date":"2026-05-15","time":"08:30","name":"PPI — April 2026",
     "currency":"USD","importance":4,"color":"#c0392b","source":"basani_macro","forecast":"","previous":"","actual":""},
    {"type":"earnings","date":"2026-05-05","ticker":"AMD","name":"AMD Q1 2026 Earnings (After Close)",
     "time":"after-market","eps_est":"1.27","rev_est":"9.84B","color":"#1a6bcc","source":"basani_macro"},
    {"type":"earnings","date":"2026-05-28","ticker":"NVDA","name":"NVDA Q1 2026 Earnings (After Close)",
     "time":"after-market","eps_est":"","rev_est":"","color":"#1a6bcc","source":"basani_macro"},
    # June 2026
    {"type":"economic","date":"2026-06-11","time":"14:00","name":"FOMC Rate Decision — June 11",
     "currency":"USD","importance":5,"color":"#c0392b","source":"basani_macro","forecast":"","previous":"","actual":""},
    {"type":"economic","date":"2026-06-11","time":"08:30","name":"CPI — May 2026",
     "currency":"USD","importance":5,"color":"#c0392b","source":"basani_macro","forecast":"","previous":"","actual":""},
]


if __name__ == "__main__":
    print("")
    print("=" * 52)
    print("  BASANI Calendar Feed  --  " + today_str)
    print("=" * 52)

    all_events = []
    all_events += fetch_earnings()
    all_events += fetch_economics()
    all_events += fetch_fda()
    all_events += fetch_ipos()
    all_events += fetch_uw_earnings()
    all_events += fetch_uw_economic()
    # Always inject BASANI macro events
    all_events += BASANI_MACRO_EVENTS
    print(f"  BASANI macros → {len(BASANI_MACRO_EVENTS)} hardcoded events injected")

    # Deduplicate earnings by ticker+date
    seen = set()
    deduped = []
    for e in all_events:
        key = f"{e.get('type')}-{e.get('date')}-{e.get('ticker','')}-{e.get('name','')}"
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    # Sort by date
    deduped.sort(key=lambda x: x.get("date",""))

    output = {
        "generated": now.isoformat(),
        "events":    deduped
    }
    with open(OUTPUT, "w") as f:
        json.dump(output, f, indent=2)

    print(f"  total         → {len(deduped)} events saved to calendar.json")
    print("=" * 52)
    print("")
