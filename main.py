#!/usr/bin/env python3
"""
BASANI Market Scanner — Production Backend
==========================================
Single-file backend. Deploy on Railway (railway.toml included).
Dashboard hosted on Netlify. Data stored in Supabase.

Architecture:
  FastAPI  ──── /               → dashboard HTML (primary entry point)
             ── /health          → keep-alive ping
             ── /status          → data freshness + source health
             ── /sources         → per-source health detail
             ── /trigger/{job}   → manual job trigger (no terminal needed)
             ── /logs            → last 500 structured log entries

  APScheduler ─ runs all data jobs on cron schedules (ET timezone)

  db.py ─────── upserts results into Supabase after each job

Design principles:
  - NEVER crash on API failure — always fall back to cached data
  - NEVER mix data categories (stocks ≠ futures ≠ options ≠ news)
  - ALWAYS label stale data clearly
  - ALWAYS retry with exponential backoff
  - ALWAYS log structured JSON for every job start/end/error
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

# ── Bootstrap ─────────────────────────────────────────────────────────────────
load_dotenv()
ROOT = Path(__file__).parent   # project root — all scripts live here

# ── Concurrency Guards ────────────────────────────────────────────────────────
# Prevent memory spikes and GitHub API conflicts when multiple jobs overlap.
_subprocess_sem = threading.Semaphore(2)   # max 2 Python subprocesses at once
_gh_lock        = threading.Lock()          # serialize all GitHub API writes
_job_running    = {}                        # {job_id: bool} — skip-if-already-running
_job_run_lock   = threading.Lock()         # guard for _job_running dict

def _acquire_job(job_id: str) -> bool:
    """Try to mark job as running. Returns False if already running (caller should skip)."""
    with _job_run_lock:
        if _job_running.get(job_id):
            return False
        _job_running[job_id] = True
        return True

def _release_job(job_id: str):
    with _job_run_lock:
        _job_running[job_id] = False

import db  # noqa: E402  (db.py is in the same directory as main.py)

# ── Structured Logging ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("basani")

# Ring buffer — last 500 log entries served via /logs
_log_buffer: list[dict] = []
_log_lock   = threading.Lock()

class StructuredHandler(logging.Handler):
    """Captures every log record into the in-memory ring buffer."""
    def emit(self, record):
        entry = {
            "ts":    datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S"),
            "lvl":   record.levelname,
            "msg":   record.getMessage(),
            "job":   getattr(record, "job", "system"),
        }
        with _log_lock:
            _log_buffer.append(entry)
            if len(_log_buffer) > 500:
                _log_buffer.pop(0)

logging.getLogger().addHandler(StructuredHandler())


# ── Source Health Registry ────────────────────────────────────────────────────
# Tracks per-source status so the dashboard and /sources endpoint can report
# which data providers are healthy, degraded, or failed.

_sources: dict[str, dict] = {
    "alpaca":    {"status": "unknown", "last_ok": None, "last_error": None, "latency_ms": None, "items": 0},
    "massive":   {"status": "unknown", "last_ok": None, "last_error": None, "latency_ms": None, "items": 0},
    "benzinga":  {"status": "unknown", "last_ok": None, "last_error": None, "latency_ms": None, "items": 0},
    "uw":        {"status": "unknown", "last_ok": None, "last_error": None, "latency_ms": None, "items": 0},
    "x_api":     {"status": "unknown", "last_ok": None, "last_error": None, "latency_ms": None, "items": 0},
}
_sources_lock = threading.Lock()

def source_ok(name: str, items: int = 0, latency_ms: float = 0):
    with _sources_lock:
        _sources[name] = {
            "status":     "healthy",
            "last_ok":    datetime.now(timezone.utc).isoformat(),
            "last_error": _sources.get(name, {}).get("last_error"),
            "latency_ms": round(latency_ms),
            "items":      items,
        }

def source_fail(name: str, error: str):
    with _sources_lock:
        _sources[name] = {
            "status":     "failed",
            "last_ok":    _sources.get(name, {}).get("last_ok"),
            "last_error": error[:200],
            "latency_ms": _sources.get(name, {}).get("latency_ms"),
            "items":      _sources.get(name, {}).get("items", 0),
        }


# ── Job Statistics ────────────────────────────────────────────────────────────
_job_stats: dict[str, dict] = {}

def _record_job(job_id: str, ok: bool, note: str = "", duration_s: float = 0):
    _job_stats[job_id] = {
        "last_run":   datetime.now(timezone.utc).isoformat(),
        "ok":         ok,
        "note":       note[:300],
        "duration_s": round(duration_s, 1),
    }


# ── Retry with Exponential Backoff ────────────────────────────────────────────
def with_retry(fn, job_id: str, max_attempts: int = 3, base_delay: float = 5.0):
    """
    Run fn() up to max_attempts times.
    Delays: 5s → 10s → 20s (doubles each attempt).
    Returns (success, result_or_error).
    """
    delay = base_delay
    for attempt in range(1, max_attempts + 1):
        try:
            result = fn()
            return True, result
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            log.warning("[%s] Attempt %d/%d failed: %s", job_id, attempt, max_attempts, err)
            if attempt < max_attempts:
                log.info("[%s] Retrying in %.0fs", job_id, delay)
                time.sleep(delay)
                delay *= 2
    return False, err


# ── GitHub Data Push ──────────────────────────────────────────────────────────
# Pushes JSON data files to GitHub after each successful job so the dashboard
# at scanner.teambasani.com can read live data from raw.githubusercontent.com

GITHUB_PAT  = os.environ.get("GITHUB_PAT",  "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")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "sumedhbasani/Basani")

def gh_push_file(content_bytes: bytes, repo_path: str, commit_msg: str) -> bool:
    """Push bytes directly to GitHub as repo_path. Returns True on success.

    Uses _gh_lock to serialize all pushes — prevents concurrent threads from
    fetching the same SHA and causing 409 Conflict errors.
    Retries once on 409 by fetching a fresh SHA.
    """
    import base64, urllib.request, urllib.error
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{repo_path}"
    gh_headers = {
        "Authorization": f"token {GITHUB_PAT}",
        "Content-Type":  "application/json",
        "User-Agent":    "BASANI/2.0",
    }

    def _fetch_sha() -> str:
        try:
            req = urllib.request.Request(api_url + "?ref=main", headers=gh_headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read()).get("sha", "")
        except Exception:
            return ""  # new file — no SHA needed

    def _do_put(sha: str) -> bool:
        payload = {
            "message": commit_msg,
            "content": base64.b64encode(content_bytes).decode(),
            "branch":  "main",
        }
        if sha:
            payload["sha"] = sha
        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode(),
            headers=gh_headers,
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status in (200, 201)

    with _gh_lock:
        sha = _fetch_sha()
        try:
            ok = _do_put(sha)
            if ok:
                log.info("→ GitHub push OK: %s", repo_path)
            return ok
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                # SHA was stale — fetch fresh and retry once
                log.warning("→ GitHub 409 for %s — retrying with fresh SHA", repo_path)
                try:
                    fresh_sha = _fetch_sha()
                    ok = _do_put(fresh_sha)
                    if ok:
                        log.info("→ GitHub push OK (retry): %s", repo_path)
                    return ok
                except Exception as exc2:
                    log.warning("→ GitHub retry failed for %s: %s", repo_path, exc2)
            else:
                log.warning("→ GitHub push FAILED for %s: HTTP %s", repo_path, exc.code)
        except Exception as exc:
            log.warning("→ GitHub push FAILED for %s: %s", repo_path, exc)
        return False


def gh_push_json(data, repo_path: str, commit_msg: str) -> bool:
    """Serialize data to JSON and push to GitHub."""
    return gh_push_file(json.dumps(data, indent=2).encode(), repo_path, commit_msg)


def normalize_scan(data: dict) -> dict:
    """
    Normalize scan_output.json schema to what the dashboard expects.
    scan_time  → scan_ts  (ISO-8601 UTC)
    direction  → dir      (alias added alongside original)
    regime     → derived from bull/bear ratio if missing
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    if "scan_ts" not in data:
        raw = data.get("scan_time", "")
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            data["scan_ts"] = dt.replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            data["scan_ts"] = now_iso
    if "regime" not in data:
        tickers = data.get("tickers", [])
        if tickers:
            bull = sum(1 for t in tickers if t.get("direction","").startswith("BULL") or t.get("dir","").startswith("BULL"))
            ratio = bull / len(tickers)
            data["regime"] = "BULL" if ratio >= 0.6 else "BEAR" if ratio <= 0.35 else "NEUTRAL"
        else:
            data["regime"] = "NEUTRAL"
    for t in data.get("tickers", []):
        if "dir" not in t:
            t["dir"] = t.get("direction", "NEUTRAL")
    return data


# ── Script Runner ─────────────────────────────────────────────────────────────
def run_script(name: str, timeout: int = 180, max_attempts: int = 2) -> tuple[bool, str]:
    """
    Run a Python script from ROOT directory.
    - Captures stdout + stderr
    - Hard timeout: 3 minutes per script
    - Retries once on failure (10s delay) before giving up
    - Returns (success, output_snippet)
    - NEVER raises — all exceptions are caught and logged
    """
    script = ROOT / name
    if not script.exists():
        msg = f"Script not found: {script}"
        log.error(msg)
        return False, msg

    last_output = ""
    for attempt in range(1, max_attempts + 1):
        log.info("▶ START  %s (attempt %d/%d)", name, attempt, max_attempts)
        t0 = time.time()
        try:
            with _subprocess_sem:   # max 2 subprocesses at once — prevent OOM
                result = subprocess.run(
                    [sys.executable, str(script)],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env={**os.environ},   # pass all env vars (API keys) to subprocess
                )
            elapsed      = time.time() - t0
            last_output  = (result.stdout + result.stderr)[-600:].strip()
            if result.returncode == 0:
                log.info("✓ DONE   %s (%.1fs)", name, elapsed)
                return True, last_output
            else:
                log.error("✗ FAIL   %s exit=%d (%.1fs): %s",
                          name, result.returncode, elapsed, last_output[-300:])
        except subprocess.TimeoutExpired:
            log.error("✗ TIMEOUT %s (>%ds)", name, timeout)
            last_output = "timeout"
        except Exception as exc:
            log.exception("✗ ERROR  %s: %s", name, exc)
            last_output = str(exc)

        if attempt < max_attempts:
            log.info("↺ RETRY  %s in 10s", name)
            time.sleep(10)

    return False, last_output


def read_json(filename: str):
    """Safely read a JSON file from ROOT. Returns None on any error."""
    path = ROOT / filename
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        log.warning("Could not read %s: %s", filename, exc)
        return None


# ── Data Jobs ─────────────────────────────────────────────────────────────────
# Each job:
#   1. Runs the relevant script (with timeout)
#   2. Reads the output JSON file
#   3. Validates the output has real content
#   4. Upserts to Supabase
#   5. Updates source health registry
#   6. Records job stats
# If ANY step fails, cached data stays in Supabase — no empty UI.

def job_news():
    t0 = time.time()
    log.info("=== NEWS JOB START ===", extra={"job": "news"})
    ok, snippet = run_script("news_feed.py")
    elapsed = time.time() - t0

    if ok:
        data = read_json("news.json")
        articles = data if isinstance(data, list) else (data.get("items") or data.get("articles") or [] if isinstance(data, dict) else [])
        items = len(articles)
        if data and items > 0:
            db.upsert("news", data)
            source_ok("benzinga", items=items, latency_ms=elapsed * 1000)
            source_ok("x_api",   items=items, latency_ms=elapsed * 1000)
            log.info("✓ News: %d items written to Supabase", items)
            # Push to GitHub
            ts_label = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
            gh_push_json(data, "news.json", f"auto: news {ts_label}")
        else:
            log.warning("News ran but returned 0 items — keeping cached data")
            source_fail("benzinga", "0 items returned")
    else:
        source_fail("benzinga", snippet[:100])
        source_fail("x_api",   snippet[:100])
        log.error("News job failed — cached Supabase data preserved")

    _record_job("news", ok, snippet[:100], elapsed)


def job_scan():
    if not _acquire_job("scan"):
        log.warning("scan already running — skipping this invocation")
        return
    try:
        _job_scan_inner()
    finally:
        _release_job("scan")

def _job_scan_inner():
    t0 = time.time()
    log.info("=== SCAN JOB START ===", extra={"job": "scan"})
    ok, snippet = run_script("scan.py")
    elapsed = time.time() - t0

    if ok:
        data = read_json("scan_output.json")
        tickers = data.get("tickers", []) if isinstance(data, dict) else []
        if data and len(tickers) > 0:
            db.upsert("scan", data)
            source_ok("alpaca", items=len(tickers), latency_ms=elapsed * 1000)
            log.info("✓ Scan: %d tickers written to Supabase", len(tickers))
            # Push normalized data to GitHub so dashboard can read it
            norm = normalize_scan(dict(data))
            ts_label = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
            gh_push_json(norm, "data.json", f"auto: scan {ts_label}")
        else:
            log.warning("Scan ran but returned 0 tickers — keeping cached data")
            source_fail("alpaca", "0 tickers returned")
    else:
        source_fail("alpaca", snippet[:100])
        log.error("Scan job failed — cached Supabase data preserved")

    _record_job("scan", ok, snippet[:100], elapsed)


def job_whales():
    if not _acquire_job("whales"):
        log.warning("whales already running — skipping this invocation")
        return
    try:
        _job_whales_inner()
    finally:
        _release_job("whales")

def _job_whales_inner():
    t0 = time.time()
    log.info("=== UW JOB START ===", extra={"job": "whales"})
    ok, snippet = run_script("uw_client.py")
    elapsed = time.time() - t0

    if ok:
        data = read_json("unusual_whales.json")
        items = len(data) if isinstance(data, list) else 0
        if data is not None and items >= 0:
            db.upsert("whales", data)
            source_ok("uw", items=items, latency_ms=elapsed * 1000)
            log.info("✓ UW: %d alerts written to Supabase", items)
            ts_label = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
            gh_push_json(data, "unusual_whales.json", f"auto: whales {ts_label}")
        else:
            source_fail("uw", "no data returned")
    else:
        source_fail("uw", snippet[:100])
        log.error("UW job failed — cached Supabase data preserved")

    _record_job("whales", ok, snippet[:100], elapsed)


def job_calendar():
    t0 = time.time()
    log.info("=== CALENDAR JOB START ===", extra={"job": "calendar"})
    ok, snippet = run_script("calendar_feed.py")
    elapsed = time.time() - t0

    if ok:
        data = read_json("calendar.json")
        events = data.get("events", []) if isinstance(data, dict) else []
        if data:
            db.upsert("calendar", data)
            log.info("✓ Calendar: %d events written to Supabase", len(events))
            ts_label = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
            gh_push_json(data, "calendar.json", f"auto: calendar {ts_label}")
    else:
        log.error("Calendar job failed — cached Supabase data preserved")

    _record_job("calendar", ok, snippet[:100], elapsed)


def job_massive():
    if not _acquire_job("massive"):
        log.warning("massive already running — skipping this invocation")
        return
    try:
        _job_massive_inner()
    finally:
        _release_job("massive")

def _job_massive_inner():
    t0 = time.time()
    log.info("=== MASSIVE JOB START ===", extra={"job": "massive"})
    ok, snippet = run_script("massive_feed.py")
    elapsed = time.time() - t0

    if ok:
        data = read_json("massive_data.json")
        snaps = data.get("snapshots", []) if isinstance(data, dict) else []
        if data and len(snaps) > 0:
            db.upsert("massive", data)
            source_ok("massive", items=len(snaps), latency_ms=elapsed * 1000)
            log.info("✓ Massive: %d snapshots written to Supabase", len(snaps))
        else:
            source_fail("massive", "0 snapshots returned")
    else:
        source_fail("massive", snippet[:100])
        log.error("Massive job failed — cached Supabase data preserved")

    _record_job("massive", ok, snippet[:100], elapsed)


def job_health():
    """
    Self-diagnosis every 10 minutes.
    Checks data freshness in Supabase.
    Writes health summary back to Supabase so dashboard can display it.
    """
    log.info("=== HEALTH CHECK ===", extra={"job": "health"})
    ages   = db.get_all_keys()
    now    = datetime.now(timezone.utc)
    issues = []

    thresholds = {
        "news":     30,       # stale after 30 min
        "scan":     30,       # stale after 30 min
        "whales":   30,       # stale after 30 min
        "calendar": 60 * 24,  # stale after 24 hours
        "massive":  30,
    }

    freshness = {}
    for key, threshold_min in thresholds.items():
        ts_str = ages.get(key)
        if not ts_str:
            issues.append(f"{key}: never written")
            freshness[key] = {"status": "missing", "age_min": None}
            continue
        try:
            ts      = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age_min = round((now - ts).total_seconds() / 60, 1)
            status  = "ok" if age_min <= threshold_min else "stale"
            freshness[key] = {"status": status, "age_min": age_min}
            if status == "stale":
                issues.append(f"{key}: {age_min}m old (limit {threshold_min}m)")
        except Exception:
            freshness[key] = {"status": "error", "age_min": None}

    with _sources_lock:
        sources_snapshot = dict(_sources)

    health_payload = {
        "checked_at":  now.isoformat(),
        "issues":      issues,
        "freshness":   freshness,
        "sources":     sources_snapshot,
        "job_stats":   dict(_job_stats),
    }

    db.upsert("health", health_payload)

    if issues:
        log.warning("HEALTH ISSUES (%d): %s", len(issues), "; ".join(issues))
    else:
        log.info("Health check: all data fresh ✓")

    _record_job("health", len(issues) == 0, "; ".join(issues) if issues else "ok")

    # Update agent communication channel on GitHub so market-agent can read status
    try:
        with _sources_lock:
            src_snap = dict(_sources)
        channel_update = {
            "_schema": "basani-agent-channel-v1",
            "directives": {
                "_updated": now.isoformat(),
                "checks": ["github_data_freshness","backend_health","news_feed_freshness","twitter_api_health","unusual_whales_health","scan_output_validity"],
                "alert_thresholds": {"scan_data_stale_minutes": 20, "news_data_stale_minutes": 45, "backend_ping_timeout_seconds": 10},
                "notes": "Written by main.py health job"
            },
            "status": {
                "_updated": now.isoformat(),
                "_agent": "main-backend",
                "overall": "ok" if not issues else "degraded",
                "checks": {
                    "backend": {"ok": True, "detail": "backend writing this"},
                    "github_data": {"ok": "scan" not in " ".join(issues), "detail": freshness.get("scan", {}).get("status","unknown")},
                    "news_feed": {"ok": "news" not in " ".join(issues), "detail": freshness.get("news", {}).get("status","unknown")},
                    "unusual_whales": {"ok": "whales" not in " ".join(issues), "detail": freshness.get("whales", {}).get("status","unknown")},
                },
                "freshness": freshness,
                "sources": src_snap,
                "summary": f"{len(issues)} issue(s)" if issues else "all data fresh"
            },
            "alerts": {
                "_updated": now.isoformat(),
                "_agent": "main-backend",
                "active": [{"check": i, "ts": now.isoformat(), "severity": "warn"} for i in issues],
                "history": []
            },
            "log": [{"ts": now.isoformat()[:19], "level": "WARN" if issues else "INFO",
                     "msg": f"Health: {'issues: ' + '; '.join(issues) if issues else 'all clear'}"}]
        }
        gh_push_json(channel_update, "_agent_channel.json", f"auto: health {now.strftime('%Y-%m-%dT%H:%MZ')}")
    except Exception as exc:
        log.warning("Could not update agent channel: %s", exc)


def job_report():
    log.info("=== DAILY REPORT ===", extra={"job": "report"})
    t0 = time.time()
    ok, snippet = run_script("daily_report.py")
    _record_job("report", ok, snippet[:100], time.time() - t0)


# ── Scheduler ─────────────────────────────────────────────────────────────────
scheduler = BackgroundScheduler(
    timezone="US/Eastern",
    job_defaults={"misfire_grace_time": 600, "coalesce": True}
)

def _on_job_event(event):
    if event.exception:
        log.error("Scheduler caught exception in job %s: %s", event.job_id, event.exception)

scheduler.add_listener(_on_job_event, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)

# News — always running, more frequent during market hours
scheduler.add_job(job_news, "cron", id="news_mkt", day_of_week="mon-fri", hour="8-17",  minute="*/5")
scheduler.add_job(job_news, "cron", id="news_eve", day_of_week="mon-fri", hour="18-23", minute="0,30")
scheduler.add_job(job_news, "cron", id="news_am",  day_of_week="mon-fri", hour="0-7",   minute="0,30")
scheduler.add_job(job_news, "cron", id="news_wkd", day_of_week="sat,sun",               minute="0,30")

# Prices + scan (market hours)
scheduler.add_job(job_scan,    "cron", id="scan_live",  day_of_week="mon-fri", hour="9-15",  minute="*/5")
scheduler.add_job(job_scan,    "cron", id="scan_pre",   day_of_week="mon-fri", hour="8",     minute="30")
scheduler.add_job(job_scan,    "cron", id="scan_power", day_of_week="mon-fri", hour="15",    minute="30")

# Massive — raw market data (prices, candles, options chains)
scheduler.add_job(job_massive, "cron", id="massive_live", day_of_week="mon-fri", hour="9-15", minute="*/5")
scheduler.add_job(job_massive, "cron", id="massive_pre",  day_of_week="mon-fri", hour="8",    minute="30")

# Options flow (market hours)
scheduler.add_job(job_whales, "cron", id="uw_live", day_of_week="mon-fri", hour="9-15", minute="*/5")

# Calendar (daily pre-market)
scheduler.add_job(job_calendar, "cron", id="cal_daily", day_of_week="mon-fri", hour="7", minute="30")

# Daily report (after close)
scheduler.add_job(job_report, "cron", id="report", day_of_week="mon-fri", hour="15", minute="45")

# Health check every 10 minutes, always
scheduler.add_job(job_health, "interval", id="health", minutes=10)


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="BASANI Scanner API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    log.info("BASANI backend starting — ROOT=%s", ROOT)
    scheduler.start()
    log.info("Scheduler started: %d jobs registered", len(scheduler.get_jobs()))
    # Immediate warm-up on deploy: run all core jobs immediately so
    # dashboard isn't empty after a cold start
    threading.Thread(target=job_news,     daemon=True, name="warmup-news").start()
    threading.Thread(target=job_calendar, daemon=True, name="warmup-cal").start()
    threading.Thread(target=job_health,   daemon=True, name="warmup-health").start()
    # Delay scan + whales slightly so news completes first (avoid race on Supabase writes)
    def _delayed_warmup():
        time.sleep(5)
        threading.Thread(target=job_scan,   daemon=True, name="warmup-scan").start()
        threading.Thread(target=job_whales, daemon=True, name="warmup-whales").start()
    threading.Thread(target=_delayed_warmup, daemon=True, name="warmup-delay").start()


@app.on_event("shutdown")
def shutdown():
    scheduler.shutdown(wait=False)
    log.info("Scheduler shut down")


@app.get("/health")
def health():
    """Health check endpoint. Must return 200 for service to stay alive."""
    return {
        "status":   "ok",
        "jobs":     len(scheduler.get_jobs()),
        "uptime":   datetime.now(timezone.utc).isoformat(),
    }


@app.get("/status")
def status():
    """Full data freshness + job stats + next scheduled runs."""
    with _sources_lock:
        src = dict(_sources)
    return {
        "data_ages":  db.get_all_keys(),
        "job_stats":  dict(_job_stats),
        "sources":    src,
        "next_runs":  {
            j.id: j.next_run_time.isoformat() if j.next_run_time else None
            for j in scheduler.get_jobs()
        },
    }


@app.get("/sources")
def sources():
    """Per-source health status — used by dashboard health panel."""
    with _sources_lock:
        return dict(_sources)


@app.post("/trigger/{job_id}")
def trigger(job_id: str):
    """
    Trigger any job manually via HTTP POST — no terminal needed.
    Use from browser, Railway shell, or any HTTP client.

    Valid job IDs: news, scan, whales, calendar, massive, report, health
    """
    fn_map = {
        "news":     job_news,
        "scan":     job_scan,
        "whales":   job_whales,
        "calendar": job_calendar,
        "massive":  job_massive,
        "report":   job_report,
        "health":   job_health,
    }
    fn = fn_map.get(job_id)
    if not fn:
        return {"error": f"Unknown job '{job_id}'", "valid": list(fn_map)}
    threading.Thread(target=fn, daemon=True, name=f"trigger-{job_id}").start()
    return {"triggered": job_id, "time": datetime.now(timezone.utc).isoformat()}


@app.get("/logs")
def logs(n: int = 100):
    """Return last N structured log entries. Max 500."""
    n = min(n, 500)
    with _log_lock:
        return list(_log_buffer[-n:])


@app.get("/", response_class=Response)
def serve_dashboard():
    """Serve the BASANI dashboard HTML — primary entry point."""
    from fastapi.responses import RedirectResponse
    html_path = ROOT / "index.html"
    if html_path.exists():
        return Response(content=html_path.read_bytes(), media_type="text/html")
    # Fallback: redirect to the GitHub raw URL
    return RedirectResponse(
        "https://raw.githubusercontent.com/sumedhbasani/Basani/main/index.html"
    )


# ── Dev entrypoint ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        reload=False,
    )
