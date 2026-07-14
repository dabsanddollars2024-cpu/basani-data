#!/usr/bin/env python3
"""
BASANI Freshness Monitor — alerts Telegram if dashboard data goes stale.

Checks every 5 min:
  - Latest scan_output.json scan_time on disk
  - Latest pusher.log activity
  - If scan_time is older than 30 min during market hours → alert
  - If pusher hasn't run in 15 min → alert

Also checks GitHub for fresh push to dabsanddollars2024-cpu/basani-data.
"""
import os, json, time, urllib.request, urllib.parse, urllib.error
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("/home/client_4319_1/basani_logs/freshness_monitor.log"),
              logging.StreamHandler()])
log = logging.getLogger("freshness")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "") or os.environ.get("BASANI_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8190762276")
SCAN_FILE = "/home/client_4319_1/basani_live/scan_output.json"
PUSHER_LOG = "/home/client_4319_1/basani_logs/pusher.log"
SCANNER_LOG = "/home/client_4319_1/basani_live/scanner.log"
DATA_REPO = "dabsanddollars2024-cpu/basani-data"

# State file: tracks last alert time so we don't spam
STATE_FILE = "/home/client_4319_1/basani_logs/.freshness_state"
ALERT_COOLDOWN_SEC = 30 * 60  # Don't repeat the same alert within 30 min


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN:
        log.warning("No Telegram token, skipping alert")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            log.info("Telegram sent: %s", r.status)
    except Exception as e:
        log.error("Telegram send failed: %s", e)


def is_market_hours() -> bool:
    """Mon-Fri 9:00-16:30 ET ≈ 13:00-20:30 UTC (rough; doesn't handle holidays)."""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    h = now.hour + now.minute / 60
    return 13.0 <= h <= 20.5


def get_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_state(s: dict):
    """Atomic write."""
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(s, f)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log.warning("could not save state: %s", e)


def get_scan_age_min() -> float | None:
    if not os.path.exists(SCAN_FILE):
        return None
    try:
        with open(SCAN_FILE) as f:
            d = json.load(f)
        st = d.get("scan_time") or d.get("generated")
        if not st:
            return None
        # Try multiple formats
        scan_dt = None
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"]:
            try:
                scan_dt = datetime.strptime(st, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if scan_dt is None:
            log.warning(f"unparseable scan_time: {st}")
            return None
        return (datetime.now(timezone.utc) - scan_dt).total_seconds() / 60
    except Exception as e:
        log.warning("get_scan_age_min error: %s", e)
        return None


def get_pusher_age_min() -> float | None:
    if not os.path.exists(PUSHER_LOG):
        return None
    try:
        # Last line that looks like a timestamped log entry
        with open(PUSHER_LOG) as f:
            lines = f.readlines()
        for line in reversed(lines[-50:]):
            if line.startswith("["):
                # Extract timestamp
                ts_str = line[1:line.find("]")]
                try:
                    last_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    return (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                except ValueError:
                    continue
        return None
    except Exception as e:
        log.warning("get_pusher_age_min error: %s", e)
        return None


def get_github_push_age_min() -> float | None:
    """Check if dabsanddollars2024-cpu/basani-data was updated recently."""
    try:
        url = f"https://api.github.com/repos/{DATA_REPO}/commits?per_page=1"
        req = urllib.request.Request(url, headers={"User-Agent": "freshness-monitor/2.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if data:
            from datetime import datetime as dt
            commit_date = data[0]["commit"]["author"]["date"]
            commit_dt = dt.strptime(commit_date, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - commit_dt).total_seconds() / 60
    except Exception as e:
        log.warning("github check error: %s", e)
    return None


def should_alert(state: dict, key: str) -> bool:
    last = state.get(key, 0)
    return (time.time() - last) > ALERT_COOLDOWN_SEC


def main():
    log.info("=== Freshness check ===")
    state = get_state()
    now = time.time()

    scan_age = get_scan_age_min()
    pusher_age = get_pusher_age_min()
    gh_age = get_github_push_age_min()

    market = is_market_hours()
    log.info(f"market_hours={market} scan_age={scan_age}pusher_age={pusher_age}gh_age={gh_age}")

    alerts = []

    # During market hours, scan should be < 30 min old
    if market and scan_age is not None and scan_age > 30:
        msg = f"⚠️ BASANI: scan_output.json is {scan_age:.0f}min old (threshold 30)"
        alerts.append(("scan_stale", msg))

    # Pusher should run every ~5 min
    if pusher_age is not None and pusher_age > 15:
        msg = f"⚠️ BASANI: pusher hasn't run in {pusher_age:.0f}min"
        alerts.append(("pusher_stale", msg))

    # GitHub should have push in last 10 min during market hours
    if market and gh_age is not None and gh_age > 10:
        msg = f"⚠️ BASANI: GitHub data repo last push was {gh_age:.0f}min ago"
        alerts.append(("gh_stale", msg))

    # Send alerts (with cooldown)
    for key, msg in alerts:
        if should_alert(state, key):
            log.warning(msg)
            send_telegram(msg)
            state[key] = now
        else:
            log.info(f"alert '{key}' suppressed (cooldown)")

    # Outside market hours, skip staleness alerts but still log
    if not market and not alerts:
        log.info("Outside market hours — skipping staleness alerts")

    save_state(state)


if __name__ == "__main__":
    main()