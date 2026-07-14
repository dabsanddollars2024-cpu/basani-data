#!/usr/bin/env python3
"""
BASANI Pusher — pushes local JSON files to dabsanddollars2024-cpu/basani-data
on GitHub. Only pushes files that have actually changed. Rate-limit aware.

Runs every 5 minutes via cron. Single source of truth for pushing scanner
output to the dashboard's data repo.
"""
import os, json, base64, hashlib, time, sys
import urllib.request as ur
import urllib.error
from datetime import datetime, timezone

SCAN_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = "/home/client_4319_1/basani_logs/pusher.log"
STATE_FILE = os.path.join(SCAN_DIR, ".pusher_state.json")
TOKEN_FILE = os.path.join(SCAN_DIR, ".gh_token")

# --- Load GitHub token ---
GITHUB_TOKEN = (
    os.environ.get("GH_TOKEN") or
    os.environ.get("GITHUB_TOKEN") or
    os.environ.get("GITHUB_PAT") or
    ""
)
if not GITHUB_TOKEN and os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE) as f:
        GITHUB_TOKEN = f.read().strip()

if not GITHUB_TOKEN:
    log_msg("ERROR: No GitHub token. Set GH_TOKEN env or create .gh_token")
    sys.exit(1)

REPO = "dabsanddollars2024-cpu/basani-data"
API = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "Content-Type": "application/json",
    "User-Agent": "basani-pusher/2.0",
}

FILES_TO_PUSH = [
    "gex_data.json",
    "scan_output.json",
    "unusual_whales.json",
    "news.json",
    "calendar.json",
    "grades.json",
    "plays.json",
    "plays_log.json",
    "data.json",
]

def log_msg(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def load_state():
    """Atomic load — return empty dict if file is missing or corrupt."""
    if not os.path.exists(STATE_FILE):
        return {"hashes": {}, "rate_limited_until": 0, "last_run": ""}
    try:
        size = os.path.getsize(STATE_FILE)
        if size == 0:
            log_msg("warn: state file is empty, starting fresh")
            return {"hashes": {}, "rate_limited_until": 0, "last_run": ""}
        with open(STATE_FILE) as f:
            data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("state not a dict")
            return data
    except (json.JSONDecodeError, ValueError, OSError) as e:
        log_msg(f"warn: corrupt state file ({e}), starting fresh")
        return {"hashes": {}, "rate_limited_until": 0, "last_run": ""}

def save_state(state):
    """Atomic write — write to .tmp then rename."""
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log_msg(f"warn: could not save state: {e}")

def api_get(url):
    try:
        req = ur.Request(url, headers=HEADERS)
        resp = ur.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return {"_rate_limited": True}
        if e.code == 404:
            return None
        log_msg(f"  API GET {e.code}: {url[:80]}")
        return None
    except Exception as e:
        log_msg(f"  API GET error: {e}")
        return None

def api_put(url, payload):
    try:
        req = ur.Request(url, data=json.dumps(payload).encode(),
                         headers=HEADERS, method="PUT")
        resp = ur.urlopen(req, timeout=20)
        return {"status": resp.status}
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        return {"error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"error": str(e)}

def push_file(filename, content_bytes, msg, state):
    h = hashlib.md5(content_bytes).hexdigest()

    # Skip if hash unchanged
    if state["hashes"].get(filename) == h:
        return "skipped"

    url = f"{API}/repos/{REPO}/contents/{filename}"
    existing = api_get(url)

    if existing and existing.get("_rate_limited"):
        state["rate_limited_until"] = time.time() + 300
        log_msg("  rate limited, backing off 5 min")
        return "rate_limited"

    sha = existing.get("sha") if existing else None

    if existing:
        remote_content = existing.get("content")
        if isinstance(remote_content, str) and remote_content:
            try:
                remote_bytes = base64.b64decode(remote_content)
                if hashlib.md5(remote_bytes).hexdigest() == h:
                    state["hashes"][filename] = h
                    return "skipped"
            except Exception:
                pass

    payload = {
        "message": msg,
        "content": base64.b64encode(content_bytes).decode(),
    }
    if sha:
        payload["sha"] = sha

    result = api_put(url, payload)
    if "error" in result:
        err = str(result["error"])
        if "403" in err or "rate limit" in err.lower():
            state["rate_limited_until"] = time.time() + 300
            log_msg(f"  rate limited on {filename}, backing off 5 min")
            return "rate_limited"
        log_msg(f"  ERROR {filename}: {err}")
        return "error"

    state["hashes"][filename] = h
    log_msg(f"  pushed {filename} ({len(content_bytes)} bytes)")
    return "pushed"

def main():
    state = load_state()
    now = time.time()

    if now < state.get("rate_limited_until", 0):
        remaining = int(state["rate_limited_until"] - now)
        log_msg(f"rate limited, {remaining}s remaining. Bailing.")
        return

    state["last_run"] = datetime.now(timezone.utc).isoformat()

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    pushed = 0
    skipped = 0
    errors = 0

    for filename in FILES_TO_PUSH:
        if time.time() < state.get("rate_limited_until", 0):
            break

        filepath = os.path.join(SCAN_DIR, filename)
        if not os.path.exists(filepath):
            continue

        try:
            with open(filepath, "rb") as f:
                content = f.read()
        except OSError as e:
            log_msg(f"  ERROR {filename}: read error {e}")
            errors += 1
            continue

        if len(content) < 10:
            continue

        result = push_file(filename, content, f"basani pusher: {ts}", state)
        if result == "pushed":
            pushed += 1
        elif result == "skipped":
            skipped += 1
        elif result == "rate_limited":
            break
        else:
            errors += 1

    log_msg(f"Done: {pushed} pushed, {skipped} skipped, {errors} errors")
    save_state(state)

if __name__ == "__main__":
    main()