#!/usr/bin/env python3
"""
BASANI Live Scanner — runs on VPS, pushes to our fork.
Continuously runs scan + news during market hours, pushes immediately.
"""
import subprocess, os, sys, time, json, base64, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
from threading import Thread

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
GITHUB_PAT = "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"
FORK_REPO   = "dabsanddollars2024-cpu/basani-data"
API_BASE   = "https://api.github.com"

DATA_FILES = [
    "scan_output.json",
    "news.json",
    "unusual_whales.json",
    "calendar.json",
    "grades.json",
    "plays_log.json",
]

def et_now():
    utc = datetime.now(timezone.utc)
    offset = -4 if 3 <= utc.month <= 10 else -5
    return utc + timedelta(hours=offset), utc

def gh(method, path, payload=None):
    url  = f"{API_BASE}/repos/{FORK_REPO}/{path}"
    hdrs = {
        "Authorization": f"token {GITHUB_PAT}",
        "Accept":        "application/vnd.github+json",
        "Content-Type":  "application/json",
        "User-Agent":    "BASANI/1.0",
    }
    body = json.dumps(payload).encode() if payload else None
    req  = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}, r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        return json.loads(raw) if raw else {}, e.code

def get_sha(remote_path):
    data, status = gh("GET", f"contents/{remote_path}")
    if status == 200:
        return data.get("sha", "")
    return ""

def push_file(local_name, remote_name=None):
    remote_name = remote_name or local_name
    local_path  = os.path.join(BASE_DIR, local_name)
    if not os.path.exists(local_path):
        print(f"  [SKIP] {local_name} not found")
        return True

    with open(local_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()

    sha = get_sha(remote_name)
    payload = {
        "message": f"live: update {remote_name} [{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC]",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha

    _, status = gh("PUT", f"contents/{remote_name}", payload)
    ok = status in (200, 201)
    print(f"  {'[OK]' if ok else '[FAIL]'} {remote_name} (HTTP {status})")
    return ok

def push_all():
    print(f"\n[{datetime.utcnow().strftime('%H:%M:%S')}] Pushing to fork...")
    ok_count = 0
    for fname in DATA_FILES:
        if push_file(fname):
            ok_count += 1
    print(f"  {ok_count}/{len(DATA_FILES)} pushed")
    return ok_count == len(DATA_FILES)

def run(script, label=None):
    label = label or script
    print(f"\n  === {label} ===")
    r = subprocess.run(["python3", script], capture_output=False)
    ok = r.returncode == 0
    print(f"  {'[OK]' if ok else '[FAIL]'} {label}")
    return ok

def main():
    print("BASANI Live Scanner starting...")
    while True:
        et, utc = et_now()
        h   = et.hour
        m   = et.minute
        wd  = et.weekday()
        is_weekday     = wd < 5
        is_premarket   = is_weekday and (8 <= h < 9 or (h == 9 and m < 30))
        is_market_open = is_weekday and (h > 9 or (h == 9 and m >= 30)) and h < 16

        if is_market_open or is_premarket:
            print(f"\n[{datetime.utcnow().strftime('%H:%M:%S')} ET {h:02d}:{m:02d}] Market active — running feeds")

            # News feed always
            run("news_feed.py", "News Feed")

            # Scan during market + premarket
            if is_market_open or is_premarket:
                run("scan.py", "Market Scan")

            # UW during market
            if is_market_open:
                run("uw_client.py", "Unusual Whales")

            # Calendar premarket/off-hours
            if is_premarket or not is_weekday or h < 8:
                run("calendar_feed.py", "Calendar")

            # Push results to our fork immediately
            push_all()
        else:
            print(f"\n[{datetime.utcnow().strftime('%H:%M:%S')} ET {h:02d}:{m:02d}] Market closed — sleeping 5min")

        time.sleep(300)  # 5 min between scan cycles

if __name__ == "__main__":
    main()
