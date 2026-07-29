#!/usr/bin/env python3
"""
BASANI News Aggregator v4 - UW-only

Source priority (all free or UW-bundled):
  1. Unusual Whales News   (official key)
  2. Stocktwits            (free, real trader sentiment)
  3. RSS fallbacks         (MarketWatch, CNBC, Yahoo, ZeroHedge, SeekingAlpha)
  4. Forex Factory         (economic calendar)

Removed in v4 (no longer reachable):
  - X / Twitter API v2     (401 since 2026-05-19)
  - Benzinga REST API      (401)
  - Massive / Polygon News (401)
  - Truth Social (Trump)   (endpoint dropped)

Features:
  - Source health monitor (status, latency, items_pulled, last_error)
  - Unified data model with impact scoring (HIGH/MED/LOW)
  - Ticker detection and tagging
  - Deduplication by URL hash + title hash
  - No single source failure breaks the run
"""

import json, os, re, ssl, hashlib, time, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
import sys
sys.path.insert(0, str(BASE_DIR))
from uw_http import uw_get_json

OUTPUT = BASE_DIR / "news.json"

# ── SSL (some RSS feeds run on weak certs) ───────────────────────────────────
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/rss+xml, application/json, text/xml, */*"}

# Tickers we tag automatically
WATCHED_TICKERS = {
    "SPY","QQQ","NVDA","TSLA","AAPL","MSFT","AMD","META","AMZN","GOOGL",
    "PLTR","COIN","MSTR","IWM","GLD","TLT","XLF","XLE","DIA","NFLX",
    "MU","SMCI","INTC","ARM","AVGO","TSM","ASML","BABA","JPM","GS",
    "BAC","JNJ","UNH","LLY","XOM","CVX","DIS","CRM","ORCL","IBM",
    "SHOP","SNAP","UBER","LYFT","RBLX","HOOD","SOFI","NIO","RIVN",
    "LCID","CHWY","DKNG","PENN","MRNA","PFE","ABBV","BMY","GILD",
    "VIX","SPX","NDX","DJIA","BTC","ETH",
}

HIGH_KEYWORDS = [
    "fed", "fomc", "rate hike", "rate cut", "rate decision", "rate hold",
    "cpi", "core cpi", "pce", "core pce", "inflation", "deflation",
    "gdp", "recession", "nfp", "non-farm payroll", "payroll",
    "jobs report", "unemployment", "jobless claims",
    "earnings beat", "earnings miss", "earnings surprise",
    "guidance raised", "guidance cut", "guidance lowered", "lowered guidance",
    "acquisition", "merger", "buyout", "takeover", "deal closed",
    "fda approval", "fda rejection", "fda decision", "clinical trial results",
    "sanctions", "tariff", "trade war", "executive order",
    "bankruptcy", "chapter 11", "default", "downgrade", "rating cut",
    "short seller", "fraud", "investigation", "sec charges", "doj",
    "powell", "yellen", "lagarde", "federal reserve", "treasury",
    "debt ceiling", "government shutdown", "war", "conflict", "attack",
    "buyback", "tender offer", "spin-off", "ipo priced",
]

MED_KEYWORDS = [
    "analyst upgrade", "analyst downgrade", "price target raised",
    "price target cut", "initiated coverage", "outperform", "underperform",
    "earnings preview", "earnings date", "quarterly results", "q1", "q2", "q3", "q4",
    "contract win", "partnership", "strategic deal", "collaboration",
    "breaking", "alert", "flash", "urgent", "developing",
    "ism", "pmi", "retail sales", "housing starts", "building permits",
    "crude oil", "opec", "natural gas", "gold rally",
    "split", "dividend", "special dividend",
    "market open", "market close", "pre-market", "after-hours",
    "insider buying", "insider selling", "13f",
]

SOURCE_AUTHORITY = {
    "uw_news":      9,
    "cnbc":         7,
    "reuters":      7,
    "marketwatch":  6,
    "seekingalpha": 5,
    "stocktwits":   3,
    "yahoo_finance": 5,
    "zerohedge":    4,
    "investing_com": 4,
    "forex_factory": 6,
}

# ── Source Health Monitor ─────────────────────────────────────────────────────
SOURCE_HEALTH: Dict[str, dict] = {}


def update_health(source_id, status, items_pulled=0, error="", latency_ms=0):
    now_str = datetime.now(timezone.utc).isoformat()
    prev = SOURCE_HEALTH.get(source_id, {})
    h = {
        "status": status,
        "items_pulled": items_pulled,
        "latency_ms": latency_ms,
        "last_success": prev.get("last_success"),
        "last_error_ts": prev.get("last_error_ts"),
        "error": prev.get("error", ""),
    }
    if status == "healthy":
        h["last_success"] = now_str
        h["error"] = ""
    else:
        h["last_error_ts"] = now_str
        h["error"] = str(error)[:300]
    SOURCE_HEALTH[source_id] = h


def detect_tickers(text):
    found = []
    for m in re.finditer(r"\$([A-Z]{1,5})\b|(?<![A-Z])([A-Z]{2,5})(?![a-z])", text or ""):
        t = (m.group(1) or m.group(2) or "").upper()
        if t in WATCHED_TICKERS and t not in found:
            found.append(t)
    return found[:8]


def calc_impact(text, source):
    t = (text or "").lower()
    authority = SOURCE_AUTHORITY.get(source, 3)
    score = authority
    for kw in HIGH_KEYWORDS:
        if kw in t:
            score += 2.5
    for kw in MED_KEYWORDS:
        if kw in t:
            score += 0.8
    tickers = detect_tickers(text)
    score += min(len(tickers) * 0.4, 2.0)
    score = min(round(score, 1), 25.0)
    if score >= 16:
        level = "HIGH"
    elif score >= 11:
        level = "MED"
    else:
        level = "LOW"
    return score, level


def item_id(source, url="", title="", ts=""):
    if url and url not in ("#", ""):
        raw = url
    else:
        raw = f"{source}:{(title or '')[:80]}:{(ts or '')[:10]}"
    return hashlib.md5(raw.encode()).hexdigest()[:14]


items: List[dict] = []
seen_ids: set = set()


def add(raw, source, source_type):
    title = raw.get("title") or raw.get("text") or raw.get("body") or raw.get("event") or ""
    body = raw.get("description") or raw.get("summary") or ""
    if body and body == title:
        body = ""

    ts = (raw.get("time") or raw.get("timestamp")
          or raw.get("created_at") or raw.get("published_utc") or "")
    url = raw.get("url") or raw.get("link") or raw.get("article_url") or ""
    author = (raw.get("author") or raw.get("handle")
              or raw.get("display_name") or raw.get("name") or "")
    tickers = raw.get("tickers") or []
    if not tickers and raw.get("ticker"):
        tickers = [raw["ticker"]]
    if not tickers:
        tickers = detect_tickers(title + " " + body)

    score, level = calc_impact(title + " " + body, source)

    iid = item_id(source, url, title, ts)
    if iid in seen_ids:
        return
    seen_ids.add(iid)

    out = {
        "id": iid,
        "source": source,
        "source_type": source_type,
        "timestamp": ts,
        "title": title[:300],
        "body": str(body)[:400],
        "url": url,
        "author": author,
        "tickers": tickers[:8],
        "tags": [],
        "impact_score": score,
        "impact_level": level,
        "sentiment": raw.get("sentiment") or "neutral",
        "time": ts,
        "text": title[:300],
        "description": str(body)[:400],
    }
    for k in ("handle", "display_name", "platform", "priority",
              "impact", "currency", "forecast", "previous", "actual",
              "week", "avatar", "channels"):
        if raw.get(k) is not None:
            out[k] = raw[k]
    items.append(out)


def fetch(url, headers=None, timeout=12):
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read()


def parse_rss(raw):
    try:
        root = ET.fromstring(raw)
        ch = root.find("channel")
        if ch is not None:
            return ch.findall("item")
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        return (root.findall("atom:entry", ns)
                or root.findall("entry"))
    except Exception:
        return []


def item_text(el, *tags):
    for tag in tags:
        v = el.findtext(tag, "")
        if v:
            return v.strip()
    return ""


# ════════════════════════════════════════════════════════════════════════════
print()
print("=" * 60)
print("  BASANI News Feed v4  --  " + datetime.now().strftime("%Y-%m-%d %H:%M"))
print("=" * 60)


# ── 1. UNUSUAL WHALES NEWS ────────────────────────────────────────────────────
def get_uw_news():
    t0 = time.time()
    data, status = uw_get_json("/api/news/headlines", {"limit": "30"})
    if status != 200:
        update_health("uw_news", "failed", error=f"HTTP {status}")
        print(f"  FAIL UW News ({status})")
        return

    raw = data if isinstance(data, list) else data.get("data", [])
    if not isinstance(raw, list):
        update_health("uw_news", "degraded", error="no data array")
        return

    count = 0
    for a in raw:
        title = a.get("headline") or a.get("title") or ""
        if not title:
            continue
        add({
            "title": title,
            "description": (a.get("summary") or a.get("description") or "")[:300],
            "time": a.get("created_at") or a.get("published_at") or "",
            "url": a.get("url") or a.get("link") or "",
            "tickers": a.get("tickers") or [],
        }, "uw_news", "news")
        count += 1

    ms = int((time.time() - t0) * 1000)
    update_health("uw_news", "healthy", items_pulled=count, latency_ms=ms)
    print(f"  OK  UW News:        {count} articles  ({ms}ms)")


try:
    get_uw_news()
except Exception as e:
    update_health("uw_news", "failed", error=str(e))
    print(f"  FAIL UW News: {e}")


# ── 2. STOCKTWITS ─────────────────────────────────────────────────────────────
def get_stocktwits():
    t0 = time.time()
    count = 0
    tickers = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMD", "META", "MSFT", "AMZN", "PLTR"]
    for sym in tickers:
        try:
            raw = fetch(f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.json?limit=5",
                        timeout=8)
            data = json.loads(raw)
            for m in data.get("messages", []):
                body = m.get("body", "")
                if not body:
                    continue
                add({
                    "title": body[:200],
                    "body": body,
                    "time": m.get("created_at", ""),
                    "url": "https://stocktwits.com/" + m.get("symbol", sym)
                           + "/stream/" + str(m.get("id", "")),
                    "author": m.get("user", {}).get("username", ""),
                    "handle": m.get("user", {}).get("username", ""),
                    "display_name": m.get("user", {}).get("username", ""),
                    "sentiment": m.get("entities", {}).get("sentiment", "neutral")
                                  or "neutral",
                    "tickers": [sym],
                }, "stocktwits", "social")
                count += 1
        except Exception as e:
            print(f"  WARN stocktwits {sym}: {e}")
            continue
    ms = int((time.time() - t0) * 1000)
    update_health("stocktwits", "healthy" if count else "degraded",
                  items_pulled=count, latency_ms=ms)
    print(f"  OK  Stocktwits:     {count} posts  ({ms}ms)")


try:
    get_stocktwits()
except Exception as e:
    update_health("stocktwits", "failed", error=str(e))
    print(f"  FAIL Stocktwits: {e}")


# ── 3. RSS FEEDS ──────────────────────────────────────────────────────────────
RSS_FEEDS = [
    ("cnbc",        "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("marketwatch", "https://www.marketwatch.com/rss/topstories"),
    ("reuters",     "https://www.reuters.com/rss/topNews"),
    ("yahoo_finance","https://finance.yahoo.com/news/rssindex"),
    ("seekingalpha","https://seekingalpha.com/feed.xml"),
    ("zerohedge",   "https://feeds.feedburner.com/zerohedge/feed"),
    ("investing_com","https://www.investing.com/rss/news.rss"),
]
rss_health = {}
rss_total = 0


def get_rss():
    global rss_total
    for name, url in RSS_FEEDS:
        t0 = time.time()
        try:
            raw = fetch(url, timeout=10)
            entries = parse_rss(raw)
            cnt = 0
            for el in entries[:15]:
                title = item_text(el, "title")
                desc = item_text(el, "description", "summary", "content")
                link = item_text(el, "link")
                ts = (item_text(el, "pubDate", "published")
                      or item_text(el, "{http://www.w3.org/2005/Atom}published"))
                add({
                    "title": title,
                    "description": (desc or "")[:300],
                    "time": ts,
                    "url": link,
                }, name, "rss")
                cnt += 1
            rss_total += cnt
            rss_health[name] = {"status": "healthy", "items": cnt, "latency_ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            rss_health[name] = {"status": "failed", "error": str(e)[:100]}
    sources = {k: v.get("items", 0) for k, v in rss_health.items() if v.get("status") == "healthy"}
    update_health("rss", "healthy" if sources else "degraded",
                  items_pulled=rss_total,
                  latency_ms=sum(v.get("latency_ms", 0) for v in rss_health.values()))
    print(f"  OK  RSS Feeds:      {rss_total} items from {len(sources)}/{len(RSS_FEEDS)} sources")


try:
    get_rss()
except Exception as e:
    update_health("rss", "failed", error=str(e))
    print(f"  FAIL RSS: {e}")


# ── 4. FOREX FACTORY ──────────────────────────────────────────────────────────
FF_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json,text/html,*/*",
    "Referer": "https://www.forexfactory.com/",
    "Origin": "https://www.forexfactory.com",
}


def get_forex():
    count = 0
    t0 = time.time()
    for url, week_tag in [
        ("https://nfs.faireconomy.media/ff_calendar_thisweek.json", "this"),
        ("https://nfs.faireconomy.media/ff_calendar_nextweek.json", "next"),
    ]:
        try:
            raw = fetch(url, headers=FF_HEADERS, timeout=15)
            data = json.loads(raw)
            for ev in data:
                impact = ev.get("impact", "")
                if impact not in ("High", "Medium"):
                    continue
                add({
                    "title": ev.get("title", ""),
                    "event": ev.get("title", ""),
                    "time": ev.get("date", ""),
                    "url": "https://www.forexfactory.com/",
                    "currency": ev.get("country", ""),
                    "impact": impact,
                    "forecast": ev.get("forecast", ""),
                    "previous": ev.get("previous", ""),
                    "actual": ev.get("actual", ""),
                    "week": week_tag,
                }, "forex_factory", "calendar")
                count += 1
        except Exception as e:
            if week_tag == "this":
                print(f"  WARN Forex Factory ({week_tag}): {e}")

    ms = int((time.time() - t0) * 1000)
    update_health("forex_factory", "healthy" if count else "degraded",
                  items_pulled=count, latency_ms=ms)
    print(f"  OK  Forex Factory:  {count} events  ({ms}ms)")


try:
    get_forex()
except Exception as e:
    update_health("forex_factory", "failed", error=str(e))
    print(f"  FAIL Forex Factory: {e}")


# ── SORT + DEDUPLICATE + SAVE ─────────────────────────────────────────────────
def sort_key(item):
    t = item.get("timestamp") or item.get("time", "")
    for fmt in (
        lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp(),
        lambda s: datetime.strptime(s[:25], "%a, %d %b %Y %H:%M:%S").timestamp(),
        lambda s: datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").timestamp(),
    ):
        try:
            return fmt(t)
        except Exception:
            continue
    return 0.0


items.sort(key=sort_key, reverse=True)

by_source = {}
for it in items:
    s = it.get("source", "unknown")
    by_source[s] = by_source.get(s, 0) + 1

SOURCE_HEALTH["rss_feeds"] = {
    "status": SOURCE_HEALTH.get("rss", {}).get("status", "unknown"),
    "items_pulled": rss_total,
    "feeds": rss_health,
}

now_utc = datetime.now(timezone.utc)
output = {
    "generated": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "generated_display": datetime.now().strftime("%Y-%m-%d %H:%M ET"),
    "count": len(items),
    "source_health": SOURCE_HEALTH,
    "by_source": by_source,
    "items": items,
}

tmp = OUTPUT.with_suffix(".json.tmp")
with open(tmp, "w") as f:
    json.dump(output, f, indent=2)
os.replace(tmp, OUTPUT)

print()
print(f"  Saved {len(items)} items  ->  news.json")
print("  Breakdown: " + " | ".join(f"{k}={v}" for k, v in sorted(by_source.items())))
print()
print("  Source Health:")
for sid, h in SOURCE_HEALTH.items():
    if sid == "rss_feeds":
        continue
    status = h.get("status", "?")
    icon = ("healthy" and "[OK]") if status == "healthy" else (("[--]" if status in ("degraded", "unavailable") else "[XX]"))
    n = h.get("items_pulled", 0)
    ms = h.get("latency_ms", 0)
    err = h.get("error", "")
    line = f"  {icon}  {sid:<18} {status:<12} {n:>4} items"
    if ms:
        line += f"  {ms}ms"
    if err and status != "healthy":
        line += f"  [{err[:60]}]"
    print(line)
print("=" * 60)
