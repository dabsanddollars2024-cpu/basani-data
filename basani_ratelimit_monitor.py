#!/usr/bin/env python3
"""
BASANI Rate Limit Monitor — tracks API quota usage across providers.

Alerts Telegram when:
  - Unusual Whales: token rejected
  - X/Twitter: token expired (401)

Checks every 5 min during market hours.
"""
import os, json, time, urllib.request, urllib.parse, urllib.error
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("/home/client_4319_1/basani_logs/rate_limit_monitor.log"),
              logging.StreamHandler()])
log = logging.getLogger("ratelimit")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "") or os.environ.get("BASANI_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8190762276")

STATE_FILE = "/home/client_4319_1/basani_logs/.ratelimit_state"
ALERT_COOLDOWN_SEC = 60 * 60  # 1 hour between rate limit alerts


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN:
        log.warning("No Telegram token, skipping alert")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            log.info(f"Telegram sent: {r.status}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


def get_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                d = json.load(f)
                if isinstance(d, dict):
                    return d
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def save_state(s: dict):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(s, f)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log.warning(f"could not save state: {e}")


def check_uw_quota() -> str | None:
    """Check Unusual Whales API token validity."""
    token = os.environ.get("UW_TOKEN") or os.environ.get("UNUSUAL_WHALES_API_KEY")
    if not token:
        env_file = "/home/client_4319_1/basani_live/.env"
        if os.path.exists(env_file):
            try:
                with open(env_file) as f:
                    for line in f:
                        if "UW_TOKEN" in line and "=" in line and not line.strip().startswith("#"):
                            token = line.split("=", 1)[1].strip().strip('"').strip("'")
                            break
            except Exception:
                pass
    if not token:
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "UW-CLIENT-API-ID": "100001",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    try:
        req = urllib.request.Request("https://api.unusualwhales.com/api/news/headlines", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            return None  # token works
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return "⚠️ BASANI: UW token rejected (401) — token invalid or expired"
        log.debug(f"UW check HTTP {e.code}")
    except Exception as e:
        log.debug(f"UW check error: {e}")
    return None


def check_x_token() -> str | None:
    """Check X/Twitter bearer token validity."""
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        env_file = "/home/client_4319_1/basani_live/.env"
        if os.path.exists(env_file):
            try:
                with open(env_file) as f:
                    for line in f:
                        if "X_BEARER_TOKEN" in line and "=" in line and not line.strip().startswith("#"):
                            token = line.split("=", 1)[1].strip().strip('"').strip("'")
                            break
            except Exception:
                pass
    if not token:
        return None

    try:
        req = urllib.request.Request(
            "https://api.twitter.com/2/users/me",
            headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(req, timeout=10):
            return None  # token works
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return "⚠️ BASANI: X/Twitter bearer token expired (401)"
        log.debug(f"X check HTTP {e.code}")
    except Exception as e:
        log.debug(f"X check error: {e}")
    return None


def main():
    log.info("=== Rate limit check ===")
    state = get_state()
    now = time.time()

    for key, checker in [("uw_quota", check_uw_quota), ("x_token", check_x_token)]:
        try:
            msg = checker()
        except Exception as e:
            log.warning(f"{key} check crashed: {e}")
            continue
        if msg:
            last = state.get(key, 0)
            if (now - last) > ALERT_COOLDOWN_SEC:
                log.warning(msg)
                send_telegram(msg)
                state[key] = now
            else:
                log.info(f"alert '{key}' suppressed (cooldown)")

    save_state(state)


if __name__ == "__main__":
    main()
