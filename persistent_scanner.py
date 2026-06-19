#!/usr/bin/env python3
"""
BASANI Persistent Scanner — runs every 5 seconds, pushes live data to our fork.
Replaces GitHub Actions which has a broken push step.
"""
import subprocess, os, json, base64, urllib.request as ur, urllib.error, time
from datetime import datetime

GITHUB_TOKEN = "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"
REPO_DATA = "dabsanddollars2024-cpu/basani-data"
REPO_SRC = "sumedhbasani/Basani"
HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "Content-Type": "application/json"
}
API = "https://api.github.com"
LOG_FILE = "/home/client_4319_1/basani_logs/persistent_scanner.log"
SCAN_DIR = "/home/client_4319_1/basani_live"
INTERVAL = 5  # seconds

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def api_get(url):
    try:
        req = ur.Request(url, headers=HEADERS)
        resp = ur.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except urllib.error.HTTPError:
        return None
    except Exception as e:
        log(f"API GET error: {e}")
        return None

def api_put(url, payload):
    data = json.dumps(payload).encode()
    req = ur.Request(url, data=data, headers=HEADERS, method="PUT")
    try:
        resp = ur.urlopen(req, timeout=20)
        return {"status": resp.status}
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        return {"error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"error": str(e)}

def push_file(repo, path, content_bytes, msg):
    url = f"{API}/repos/{repo}/contents/{path}"
    existing = api_get(url)
    sha = existing.get("sha") if existing else None
    payload = {
        "message": msg,
        "content": base64.b64encode(content_bytes).decode(),
    }
    if sha:
        payload["sha"] = sha
    result = api_put(url, payload)
    if "error" in result:
        log(f"  ❌ {path}: {result['error']}")
        return False
    else:
        log(f"  ✅ {path}")
        return True

def run_scan():
    tmpdir = f"/tmp/basani_persist_{int(time.time())}"
    try:
        # Clone source repo
        r = subprocess.run(
            ["git", "clone", "--depth=1",
             f"https://x-access-token:{GITHUB_TOKEN}@github.com/{REPO_SRC}.git", tmpdir],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode != 0:
            log(f"Clone failed: {r.stderr[:150]}")
            return False

        # Run scan
        r = subprocess.run(["python3", "run_all.py"], cwd=tmpdir,
                           capture_output=True, text=True, timeout=60)
        # Don't fail if scan returns non-zero - still try to push available files
        
        ts = datetime.now().strftime("%Y-%m-%dT%H:%MZ")
        pushed = 0

        # Try to push each file if it exists
        files_to_push = [
            ("scan_output.json", f"🤖 scan: {ts}"),
            ("data.json", f"🤖 data: {ts}"),
            ("news.json", f"🤖 news: {ts}"),
            ("unusual_whales.json", f"🤖 whales: {ts}"),
            ("calendar.json", f"🤖 calendar: {ts}"),
            ("grades.json", f"🤖 grades: {ts}"),
            ("plays.json", f"🤖 plays: {ts}"),
            ("plays_log.json", f"🤖 plays_log: {ts}"),
        ]

        for filename, commit_msg in files_to_push:
            filepath = os.path.join(tmpdir, filename)
            if os.path.exists(filepath):
                with open(filepath, "rb") as f:
                    if push_file(REPO_DATA, filename, f.read(), commit_msg):
                        pushed += 1

        if pushed == 0:
            log("No files available to push (market closed)")
        else:
            log(f"Push complete: {pushed} files")
        return pushed > 0

    finally:
        subprocess.run(["rm", "-rf", tmpdir], capture_output=True)

def main():
    log("=== Persistent Scanner STARTED (5s interval) ===")
    count = 0
    while True:
        count += 1
        log(f"--- Run #{count} ---")
        run_scan()
        log(f"Sleeping {INTERVAL}s...")
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
