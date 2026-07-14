#!/usr/bin/env python3
"""
BASANI News Aggregator v3 — Professional Multi-Source Intelligence Pipeline

Source priority:
  1. X / Twitter API v2     (set X_BEARER_TOKEN env var)
  2. Benzinga REST API      (official key)
  3. Unusual Whales News    (official key)
  4. Massive / Polygon News (official key)
  5. Truth Social (Trump)   (free Mastodon-compatible API)
  6. Stocktwits             (free, real trader sentiment)
  7. RSS fallbacks          (MarketWatch, CNBC, Yahoo, ZeroHedge, SeekingAlpha)
  8. Forex Factory          (economic calendar, fallback to Benzinga calendar)

Features:
  - Source health monitor (status, latency, items_pulled, last_error)
  - Unified data model with impact scoring (HIGH/MED/LOW)
  - Ticker detection and tagging
  - Deduplication by URL hash + title hash
  - No single source failure breaks the run
"""

import json, os, re, ssl, hashlib, time, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

# ── Directory / Output ───────────────────────────────────────────────────────
DIR    = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(DIR, "news.json")

# ── API Keys ─────────────────────────────────────────────────────────────────
# Preferred: set as environment variables (export X_BEARER_TOKEN="...")
# Fallback:  hardcoded values below
X_BEARER_TOKEN = (
    os.getenv("X_BEARER_TOKEN") or
    os.getenv("TWITTER_BEARER_TOKEN") or
    "+LXABeg=xuIUNm999SZymusBqSbyy09DzIy9mt0VbeAr2Q1fVgJPbVNbj1"
)
BZ_KEY     = os.getenv("BENZINGA_API_KEY",        "TUUL5FC2AC")
UW_KEY     = os.getenv("UNUSUAL_WHALES_API_KEY",  "")
MASSIVE_KEY= os.getenv("MASSIVE_API_KEY",         "VY4k3Lj")

# ── SSL (bypass cert errors on some feeds) ───────────────────────────────────
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode    = ssl.CERT_NONE

# ── Standard headers ─────────────────────────────────────────────────────────
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/rss+xml, application/json, text/xml, */*"}

# ── Tickers to detect in text ────────────────────────────────────────────────
WATCHED_TICKERS = {
    "SPY","QQQ","NVDA","TSLA","AAPL","MSFT","AMD","META","AMZN","GOOGL",
    "PLTR","COIN","MSTR","IWM","GLD","TLT","XLF","XLE","DIA","NFLX",
    "MU","SMCI","INTC","ARM","AVGO","TSM","ASML","BABA","JPM","GS",
    "BAC","JNJ","UNH","LLY","XOM","CVX","DIS","CRM","ORCL","IBM",
    "SHOP","SNAP","UBER","LYFT","RBLX","HOOD","SOFI","NIO","RIVN",
    "LCID","CHWY","DKNG","PENN","MRNA","PFE","ABBV","BMY","GILD",
    "VIX","SPX","NDX","DJIA","BTC","ETH",
}

# ── Impact keyword weights ────────────────────────────────────────────────────
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
    "x_api":       10,
    "benzinga":     9,
    "massive_news": 8,
    "uw_news":      8,
    "cnbc":         7,
    "reuters":      7,
    "trump":        9,
    "marketwatch":  6,
    "seekingalpha": 5,
    "stocktwits":   3,
    "yahoo_finance":5,
    "zerohedge":    4,
    "investing_com":4,
    "forex_factory":6,
}

# ── Source Health Monitor ─────────────────────────────────────────────────────
SOURCE_HEALTH: Dict[str, dict] = {}

def update_health(source_id: str, status: str, items_pulled: int = 0,
                  error: str = "", latency_ms: int = 0):
    now_str = datetime.now(timezone.utc).isoformat()
    prev    = SOURCE_HEALTH.get(source_id, {})
    h = {
        "status":       status,
        "items_pulled": items_pulled,
        "latency_ms":   latency_ms,
        "last_success": prev.get("last_success"),
        "last_error_ts":prev.get("last_error_ts"),
        "error":        prev.get("error", ""),
    }
    if status == "healthy":
        h["last_success"] = now_str
        h["error"]        = ""
    else:
        h["last_error_ts"]= now_str
        h["error"]        = str(error)[:300]
    SOURCE_HEALTH[source_id] = h

# ── Utility helpers ───────────────────────────────────────────────────────────
def strip_html(text: str) -> str:
    return re.sub(r'<[^>]+>', '', text or '').strip()

def detect_tickers(text: str) -> List[str]:
    found = []
    for m in re.finditer(r'\$([A-Z]{1,5})\b|(?<![A-Z])([A-Z]{2,5})(?![a-z])', text or ''):
        t = (m.group(1) or m.group(2) or '').upper()
        if t in WATCHED_TICKERS and t not in found:
            found.append(t)
    return found[:8]  # cap at 8 tickers per item

def calc_impact(text: str, source: str) -> tuple:
    """Returns (score: float, level: str)"""
    t         = (text or '').lower()
    authority = SOURCE_AUTHORITY.get(source, 3)
    score     = authority

    for kw in HIGH_KEYWORDS:
        if kw in t:
            score += 2.5
    for kw in MED_KEYWORDS:
        if kw in t:
            score += 0.8

    tickers = detect_tickers(text)
    score  += min(len(tickers) * 0.4, 2.0)
    score   = min(round(score, 1), 25.0)

    if score >= 16:
        level = "HIGH"
    elif score >= 11:
        level = "MED"
    else:
        level = "LOW"

    return score, level

def item_id(source: str, url: str = '', title: str = '', ts: str = '') -> str:
    if url and url not in ('#', ''):
        raw = url
    else:
        raw = f"{source}:{(title or '')[:80]}:{(ts or '')[:10]}"
    return hashlib.md5(raw.encode()).hexdigest()[:14]

# ── Global items list + dedup set ─────────────────────────────────────────────
items:    List[dict] = []
seen_ids: set        = set()

def add(raw: dict, source: str, source_type: str):
    """Normalize and append item if not already seen."""
    # Extract core fields
    title  = raw.get("title") or raw.get("text") or raw.get("body") or raw.get("event") or ""
    body   = raw.get("description") or raw.get("summary") or ""
    if body and body == title:
        body = ""

    ts      = (raw.get("time") or raw.get("timestamp") or
               raw.get("created_at") or raw.get("published_utc") or "")
    url     = (raw.get("url") or raw.get("link") or raw.get("article_url") or "")
    author  = (raw.get("author") or raw.get("handle") or
               raw.get("display_name") or raw.get("name") or "")
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

    out: dict = {
        "id":           iid,
        "source":       source,
        "source_type":  source_type,
        "timestamp":    ts,
        "title":        title[:300],
        "body":         str(body)[:400],
        "url":          url,
        "author":       author,
        "tickers":      tickers[:8],
        "tags":         [],
        "impact_score": score,
        "impact_level": level,
        "sentiment":    raw.get("sentiment") or "neutral",
        # Legacy fields kept for dashboard backward-compat
        "time":         ts,
        "text":         title[:300],
        "description":  str(body)[:400],
    }
    # Preserve platform-specific extras
    for k in ("handle", "display_name", "platform", "priority",
              "impact", "currency", "forecast", "previous", "actual",
              "week", "avatar", "channels"):
        if raw.get(k) is not None:
            out[k] = raw[k]

    items.append(out)

def fetch(url: str, headers: dict = None, timeout: int = 12) -> bytes:
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read()

def parse_rss(raw: bytes) -> list:
    try:
        root = ET.fromstring(raw)
        ch   = root.find("channel")
        if ch is not None:
            return ch.findall("item")
        ns = {'atom': 'http://www.w3.org/2005/Atom'}
        return root.findall("atom:entry", ns) or root.findall("entry")
    except Exception:
        return []

def item_text(el, *tags) -> str:
    for tag in tags:
        v = el.findtext(tag, "")
        if v:
            return v.strip()
    return ""


# ════════════════════════════════════════════════════════════════════════════
print()
print("=" * 60)
print("  BASANI News Feed v3  --  " + datetime.now().strftime("%Y-%m-%d %H:%M"))
print("=" * 60)

# ── 1. X / TWITTER API v2 — Recent Search ───────────────────────────────────
# Set X_BEARER_TOKEN as an environment variable:
#   export X_BEARER_TOKEN="AAAAAAAAAAAAAAAAAAAAAy..."
#   Or add it to your shell profile: ~/.zshrc or ~/.bash_profile
#
# Accounts tracked: top financial news posters on X
X_ACCOUNTS = [
    "DeItaone",        # Walter Bloomberg / fastest headlines
    "KobeissiLetter",  # Macro analysis
    "FinancialJuice",  # Fast financial headlines
    "FirstSquawk",     # Breaking market news
    "unusual_whales",  # Options flow + political trades
    "benzinga",        # Benzinga news
    "CNBCnow",         # CNBC breaking
    "ReutersUS",       # Reuters US news
    "zerohedge",       # Alternative finance
    "markets",         # Bloomberg Markets
    "WSJ",             # Wall Street Journal
    "FT",              # Financial Times
    "federalreserve",  # Fed official account
    "MikeZaccardi",    # Rates / macro
    "PatrickMcHenry",  # House Financial Cmte
    "SenWarren",       # Senate Finance (market regulation)
    "SecBessent",      # Treasury Secretary
    "NickTimiraos",    # WSJ Fed reporter (fed whisperer)
    "EddyElfenbein",   # Stock market analysis
    "TihoBrkan",       # Technical analysis / global macro
]

def get_x_posts():
    if not X_BEARER_TOKEN:
        update_health("x_api", "unavailable",
                      error="X_BEARER_TOKEN not set — see setup instructions")
        print("  SKIP X API: X_BEARER_TOKEN not set")
        print("         → To enable: export X_BEARER_TOKEN='your_bearer_token_here'")
        print("         → Add to ~/.zshrc for permanent use")
        return

    # Build query: from any tracked account, no retweets, no replies
    from_clause = " OR ".join(f"from:{a}" for a in X_ACCOUNTS[:20])
    query = f"({from_clause}) -is:retweet -is:reply lang:en"

    # Pull last 30 min (cron runs every 10 min, 30 min window for safety)
    start = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    params = urllib.parse.urlencode({
        "query":       query,
        "max_results": 100,
        "start_time":  start,
        "tweet.fields":"created_at,author_id,text,public_metrics",
        "expansions":  "author_id",
        "user.fields": "username,name,verified,profile_image_url",
    })

    req_url = "https://api.twitter.com/2/tweets/search/recent?" + params
    t0 = time.time()
    try:
        req = urllib.request.Request(req_url, headers={
            "Authorization": "Bearer " + X_BEARER_TOKEN,
            "User-Agent":    "BASANI/3.0",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        ms = int((time.time() - t0) * 1000)

        tweets = data.get("data") or []
        users  = {u["id"]: u for u in (data.get("includes", {}).get("users") or [])}

        count = 0
        for tw in tweets:
            uid    = tw.get("author_id", "")
            user   = users.get(uid, {})
            handle = user.get("username", "")
            text   = tw.get("text", "").strip()
            if not text:
                continue
            # Strip t.co links (Twitter wraps everything)
            clean = re.sub(r'https?://t\.co/\S+', '', text).strip()
            add({
                "title":        clean or text,
                "time":         tw.get("created_at", ""),
                "url":          f"https://x.com/i/web/status/{tw.get('id','')}",
                "handle":       handle,
                "author":       user.get("name", handle),
                "display_name": user.get("name", handle),
                "avatar":       user.get("profile_image_url", ""),
                "tickers":      detect_tickers(text),
                "platform":     "x",
            }, "x_api", "x_post")
            count += 1

        update_health("x_api", "healthy", items_pulled=count, latency_ms=ms)
        print(f"  OK  X API:          {count} posts  ({ms}ms)")

    except urllib.error.HTTPError as e:
        code  = e.code
        body  = e.read().decode("utf-8", errors="replace")[:300]
        err   = f"HTTP {code}: {body}"
        update_health("x_api", "failed", error=err)
        if code == 401:
            print(f"  FAIL X API (401): Invalid bearer token — check X_BEARER_TOKEN")
        elif code == 429:
            print(f"  FAIL X API (429): Rate limit — too many requests this window")
        elif code == 403:
            print(f"  FAIL X API (403): Access denied — check your API tier (needs Basic+)")
        else:
            print(f"  FAIL X API ({code}): {body[:120]}")
    except Exception as e:
        update_health("x_api", "failed", error=str(e))
        print(f"  FAIL X API: {e}")

try:
    get_x_posts()
except Exception as e:
    print(f"  FAIL X API (outer): {e}")
    update_health("x_api", "failed", error=str(e))

# ── 2. BENZINGA NEWS API ──────────────────────────────────────────────────────
BZ_TICKERS = (
    "SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,META,AMZN,TSLA,"
    "MU,PLTR,AMD,NFLX,COIN,SMCI,MSTR,"
    "IWM,DIA,XLF,XLE,GLD,TLT"
)

def get_benzinga():
    cutoff     = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params_str = urllib.parse.urlencode({
        "token":         BZ_KEY,
        "tickers":       BZ_TICKERS,
        "dateFrom":      cutoff[:10],
        "pageSize":      50,
        "displayOutput": "full",
        "sort":          "created:desc",
    })
    url = "https://api.benzinga.com/api/v2/news?" + params_str
    t0  = time.time()
    try:
        req = urllib.request.Request(url, headers={
            "Accept":     "application/json",
            "User-Agent": "BASANI-MarketScanner/3.0",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            raw  = json.loads(r.read())
        ms       = int((time.time() - t0) * 1000)
        articles = raw if isinstance(raw, list) else raw.get("data", [])
        count    = 0
        for art in articles[:60]:
            stocks = art.get("stocks") or []
            ticks  = [s.get("name","") for s in stocks if s.get("name")] if stocks else []
            body   = re.sub(r'<[^>]+>', '', art.get("body", art.get("teaser","")) or "")[:400]
            pub    = art.get("created", art.get("updated", ""))
            add({
                "title":       art.get("title",""),
                "body":        body,
                "description": body,
                "time":        pub,
                "url":         art.get("url",""),
                "author":      art.get("author",""),
                "tickers":     ticks,
                "channels":    [c.get("name",c) if isinstance(c,dict) else str(c)
                                for c in (art.get("channels") or [])],
            }, "benzinga", "benzinga_news")
            count += 1
        update_health("benzinga", "healthy", items_pulled=count, latency_ms=ms)
        print(f"  OK  Benzinga:       {count} articles  ({ms}ms)")
    except Exception as e:
        update_health("benzinga", "failed", error=str(e))
        print(f"  FAIL Benzinga: {e}")

try:
    get_benzinga()
except Exception as e:
    print(f"  FAIL Benzinga (outer): {e}")
    update_health("benzinga", "failed", error=str(e))

# ── 3. UNUSUAL WHALES NEWS ────────────────────────────────────────────────────
def get_uw_news():
    # Try multiple known UW news endpoints
    endpoints = [
        "https://api.unusualwhales.com/api/news/headlines",
        "https://api.unusualwhales.com/api/market/news",
        "https://api.unusualwhales.com/api/news",
    ]
    t0 = time.time()
    for endpoint in endpoints:
        try:
            req = urllib.request.Request(
                endpoint,
                headers={"Authorization": "Bearer " + UW_KEY,
                         "Accept": "application/json", "User-Agent": "BASANI/3.0"}
            )
            with urllib.request.urlopen(req, timeout=12) as r:
                raw = json.loads(r.read())
            ms   = int((time.time() - t0) * 1000)
            arts = raw if isinstance(raw, list) else raw.get("data", [])
            if not isinstance(arts, list):
                continue
            count = 0
            for a in arts[:25]:
                title = a.get("headline") or a.get("title") or ""
                if not title:
                    continue
                add({
                    "title":       title,
                    "description": (a.get("summary") or a.get("description") or "")[:300],
                    "time":        a.get("created_at") or a.get("published_at") or "",
                    "url":         a.get("url") or a.get("link") or "",
                    "tickers":     a.get("tickers") or [],
                }, "uw_news", "benzinga_news")
                count += 1
            update_health("uw_news", "healthy", items_pulled=count, latency_ms=ms)
            print(f"  OK  UW News:        {count} articles  ({ms}ms)  [{endpoint.split('/')[-1]}]")
            return
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue  # try next endpoint
            update_health("uw_news", "failed", error=str(e))
            print(f"  SKIP UW News ({e.code}): {endpoint}")
            return
        except Exception as e:
            update_health("uw_news", "failed", error=str(e))
            print(f"  SKIP UW News: {e}")
            return
    update_health("uw_news", "unavailable", error="No working endpoint found")
    print(f"  SKIP UW News: no working endpoint on this plan")

try:
    get_uw_news()
except Exception as e:
    print(f"  FAIL UW News (outer): {e}")
    update_health("uw_news", "failed", error=str(e))

# ── 4. MASSIVE / POLYGON NEWS ─────────────────────────────────────────────────
def get_massive():
    ticks  = "SPY,QQQ,AAPL,MSFT,NVDA,GOOGL,META,AMZN,TSLA,PLTR,AMD,NFLX,COIN,MSTR"
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = urllib.parse.urlencode({
        "ticker.any_of":     ticks,
        "published_utc.gte": cutoff,
        "limit":             50,
        "order":             "desc",
        "sort":              "published_utc",
    })
    url = "https://api.massive.com/v2/reference/news?" + params
    t0  = time.time()
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + MASSIVE_KEY,
            "Accept":        "application/json",
        })
        with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
            raw = json.loads(r.read())
        ms   = int((time.time() - t0) * 1000)
        arts = raw.get("results", [])
        count = 0
        for a in arts:
            pub = a.get("publisher", {})
            add({
                "title":       a.get("title",""),
                "description": a.get("description","")[:300],
                "time":        a.get("published_utc",""),
                "url":         a.get("article_url", a.get("url","")),
                "tickers":     a.get("tickers",[]),
                "author":      pub.get("name","") if isinstance(pub, dict) else str(pub),
            }, "massive_news", "benzinga_news")
            count += 1
        update_health("massive_news", "healthy", items_pulled=count, latency_ms=ms)
        print(f"  OK  Massive/Polygon: {count} articles  ({ms}ms)")
    except Exception as e:
        update_health("massive_news", "failed", error=str(e))
        print(f"  FAIL Massive/Polygon: {e}")

try:
    get_massive()
except Exception as e:
    print(f"  FAIL Massive/Polygon (outer): {e}")
    update_health("massive_news", "failed", error=str(e))

# ── 5. TRUTH SOCIAL — Trump ──────────────────────────────────────────────────
def get_trump():
    # Truth Social uses Mastodon-compatible API but restricts direct access.
    # Trump's account ID is known: 107780257626128497
    TRUMP_ID = "107780257626128497"
    t0       = time.time()

    # Try multiple approaches
    attempts = [
        # Direct statuses with known ID (no lookup needed)
        f"https://truthsocial.com/api/v1/accounts/{TRUMP_ID}/statuses?limit=10&exclude_replies=true&exclude_reblogs=true",
        # Try v2 if available
        f"https://truthsocial.com/api/v2/accounts/{TRUMP_ID}/statuses?limit=10",
    ]
    hdrs = {
        "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept":          "application/json, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://truthsocial.com/@realDonaldTrump",
        "Origin":          "https://truthsocial.com",
    }

    for url in attempts:
        try:
            statuses = json.loads(fetch(url, hdrs, timeout=12))
            if not isinstance(statuses, list):
                continue
            ms    = int((time.time() - t0) * 1000)
            count = 0
            for s in statuses[:10]:
                content = strip_html(s.get("content","")).strip()
                if not content:
                    continue
                add({
                    "title":        content[:300],
                    "text":         content[:300],
                    "time":         s.get("created_at",""),
                    "url":          s.get("url", "https://truthsocial.com/@realDonaldTrump"),
                    "handle":       "realDonaldTrump",
                    "display_name": "Donald J. Trump",
                    "author":       "Donald J. Trump",
                    "priority":     "HIGH",
                    "platform":     "truthsocial",
                }, "trump", "x_post")
                count += 1
            update_health("trump", "healthy", items_pulled=count, latency_ms=ms)
            print(f"  OK  Truth Social:   {count} posts  ({ms}ms)")
            return
        except urllib.error.HTTPError as e:
            if e.code in (403, 401):
                continue
            break
        except Exception:
            break

    # If all attempts fail, skip gracefully — Truth Social API is unreliable
    update_health("trump", "unavailable", error="Truth Social API blocked (403) — posts still visible at truthsocial.com/@realDonaldTrump")
    print(f"  SKIP Truth Social: API blocked — check truthsocial.com directly")

try:
    get_trump()
except Exception as e:
    print(f"  FAIL Truth Social (outer): {e}")
    update_health("trump", "failed", error=str(e))

# ── 6. STOCKTWITS — real trader sentiment ────────────────────────────────────
# Free public API, no auth. Pulls sentiment + discussion per ticker.
STOCKTWITS_TICKERS = [
    "SPY","QQQ","NVDA","TSLA","AAPL","MSFT","AMD",
    "META","AMZN","GOOGL","PLTR","COIN","MSTR","IWM","GLD"
]

def get_stocktwits():
    count = 0
    t0    = time.time()
    for ticker in STOCKTWITS_TICKERS:
        try:
            url  = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json?limit=5"
            data = json.loads(fetch(url, timeout=8))
            for msg in data.get("messages", []):
                user      = msg.get("user", {})
                body      = msg.get("body","").strip()
                sentiment = ((msg.get("entities") or {}).get("sentiment") or {}).get("basic","")
                if not body:
                    continue
                add({
                    "title":        body[:300],
                    "text":         body[:300],
                    "time":         msg.get("created_at",""),
                    "url":          f"https://stocktwits.com/symbol/{ticker}/message/{msg.get('id','')}",
                    "handle":       user.get("username", ticker),
                    "display_name": user.get("name", user.get("username", ticker)),
                    "ticker":       ticker,
                    "sentiment":    sentiment.lower() if sentiment else "neutral",
                    "platform":     "stocktwits",
                }, "stocktwits", "x_post")
                count += 1
        except Exception:
            continue
    ms = int((time.time() - t0) * 1000)
    update_health("stocktwits", "healthy" if count else "degraded",
                  items_pulled=count, latency_ms=ms)
    print(f"  OK  Stocktwits:     {count} messages  ({ms}ms)")

try:
    get_stocktwits()
except Exception as e:
    print(f"  FAIL Stocktwits: {e}")
    update_health("stocktwits", "failed", error=str(e))

# ── 7. RSS FEEDS — fallback layer ─────────────────────────────────────────────
# These are fallback only. One bad feed must never break the others.

RSS_SOURCES = {
    "marketwatch": [
        ("https://feeds.marketwatch.com/marketwatch/topstories/", "marketwatch"),
        ("https://feeds.marketwatch.com/marketwatch/marketpulse/", "marketwatch"),
    ],
    "cnbc": [
        ("https://www.cnbc.com/id/100003114/device/rss/rss.html", "cnbc"),
        ("https://www.cnbc.com/id/15839135/device/rss/rss.html",  "cnbc"),
        ("https://www.cnbc.com/id/10000664/device/rss/rss.html",  "cnbc"),
    ],
    "yahoo_finance": [
        ("https://finance.yahoo.com/news/rssindex",        "reuters"),
        ("https://finance.yahoo.com/rss/topfinstories",    "reuters"),
    ],
    "zerohedge": [
        ("https://feeds.feedburner.com/zerohedge/feed",    "zerohedge"),
        ("https://www.zerohedge.com/fullrss2.xml",         "zerohedge"),
    ],
    "seekingalpha": [
        ("https://seekingalpha.com/market_currents.xml",              "seekingalpha"),
        ("https://seekingalpha.com/tag/wall-st-breakfast.xml",        "seekingalpha"),
    ],
    "investing_com": [
        ("https://www.investing.com/rss/news.rss",         "reuters"),
        ("https://www.investing.com/rss/news_285.rss",     "reuters"),
    ],
}

rss_total = 0
rss_health: dict = {}

for rss_id, feeds in RSS_SOURCES.items():
    feed_count = 0
    feed_err   = ""
    t0 = time.time()
    for url, badge_source in feeds:
        if feed_count >= 10:
            break
        try:
            raw   = fetch(url, timeout=10)
            if not raw.strip().startswith(b'<'):
                continue
            elems = parse_rss(raw)
            for el in elems[:8]:
                title = item_text(el, "title")
                if not title:
                    continue
                add({
                    "title":       title,
                    "description": strip_html(item_text(el, "description", "summary"))[:300],
                    "time":        item_text(el, "pubDate", "updated", "published"),
                    "url":         item_text(el, "link", "guid"),
                }, badge_source, "rss")
                feed_count += 1
            if feed_count:
                break  # got items from first working URL
        except Exception as e:
            feed_err = str(e)[:100]
            continue

    ms = int((time.time() - t0) * 1000)
    if feed_count:
        rss_health[rss_id] = {"status": "healthy", "items": feed_count, "latency_ms": ms}
        rss_total += feed_count
    else:
        rss_health[rss_id] = {"status": "failed", "items": 0, "error": feed_err}

update_health("rss", "healthy" if rss_total > 0 else "degraded",
              items_pulled=rss_total)
print(f"  OK  RSS feeds:      {rss_total} articles  "
      f"({sum(1 for v in rss_health.values() if v['status']=='healthy')}/"
      f"{len(rss_health)} feeds healthy)")

# ── 8. FOREX FACTORY — economic calendar ──────────────────────────────────────
FF_HEADERS = {
    "User-Agent": UA,
    "Accept":     "application/json, text/json, */*",
    "Referer":    "https://www.forexfactory.com/",
    "Origin":     "https://www.forexfactory.com",
}

def get_forex():
    count = 0
    t0    = time.time()
    for url, week_tag in [
        ("https://nfs.faireconomy.media/ff_calendar_thisweek.json", "this"),
        ("https://nfs.faireconomy.media/ff_calendar_nextweek.json", "next"),
    ]:
        retries = 2
        for attempt in range(retries):
            try:
                raw  = fetch(url, headers=FF_HEADERS, timeout=15)
                data = json.loads(raw)
                for ev in data:
                    impact = ev.get("impact","")
                    if impact not in ("High","Medium"):
                        continue
                    add({
                        "title":    ev.get("title",""),
                        "event":    ev.get("title",""),
                        "time":     ev.get("date",""),
                        "url":      "https://www.forexfactory.com/",
                        "currency": ev.get("country",""),
                        "impact":   impact,
                        "forecast": ev.get("forecast",""),
                        "previous": ev.get("previous",""),
                        "actual":   ev.get("actual",""),
                        "week":     week_tag,
                    }, "forex_factory", "calendar")
                    count += 1
                break  # success — don't retry
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(1.5)
                else:
                    print(f"  WARN Forex Factory ({week_tag}): {e}")

    ms = int((time.time() - t0) * 1000)
    update_health("forex_factory", "healthy" if count else "degraded",
                  items_pulled=count, latency_ms=ms)
    print(f"  OK  Forex Factory:  {count} events  ({ms}ms)")

try:
    get_forex()
except Exception as e:
    print(f"  FAIL Forex Factory: {e}")
    update_health("forex_factory", "failed", error=str(e))

# ── SORT + DEDUPLICATE + SAVE ─────────────────────────────────────────────────
def sort_key(item: dict) -> float:
    t = item.get("timestamp") or item.get("time","")
    for fmt in (
        lambda s: datetime.fromisoformat(s.replace("Z","+00:00")).timestamp(),
        lambda s: datetime.strptime(s[:25], "%a, %d %b %Y %H:%M:%S").timestamp(),
        lambda s: datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").timestamp(),
    ):
        try:
            return fmt(t)
        except Exception:
            continue
    return 0.0

items.sort(key=sort_key, reverse=True)

# Compile per-source breakdown
by_source: dict = {}
for it in items:
    s = it.get("source","unknown")
    by_source[s] = by_source.get(s, 0) + 1

# Merge RSS sub-health into main source_health
SOURCE_HEALTH["rss_feeds"] = {
    "status":       SOURCE_HEALTH.get("rss", {}).get("status", "unknown"),
    "items_pulled": rss_total,
    "feeds":        rss_health,
}

now_utc = datetime.now(timezone.utc)
output  = {
    "generated":         now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "generated_display": datetime.now().strftime("%Y-%m-%d %H:%M ET"),
    "count":             len(items),
    "source_health":     SOURCE_HEALTH,
    "by_source":         by_source,
    "items":             items,
}

with open(OUTPUT, "w") as f:
    json.dump(output, f, indent=2)

print()
print(f"  ✅  Saved {len(items)} items  →  news.json")
print("  Breakdown: " + " | ".join(f"{k}={v}" for k,v in sorted(by_source.items())))
print()
print("  Source Health:")
for sid, h in SOURCE_HEALTH.items():
    if sid == "rss_feeds":
        continue
    status = h.get("status","?")
    icon   = "🟢" if status == "healthy" else ("🟡" if status in ("degraded","unavailable") else "🔴")
    n      = h.get("items_pulled",0)
    ms     = h.get("latency_ms",0)
    err    = h.get("error","")
    line   = f"  {icon}  {sid:<18} {status:<12} {n:>4} items"
    if ms:
        line += f"  {ms}ms"
    if err and status != "healthy":
        line += f"  [{err[:60]}]"
    print(line)
print("=" * 60)
