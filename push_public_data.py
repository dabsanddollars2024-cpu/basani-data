#!/usr/bin/env python3
"""
BASANI — Push data files to public basani-data repo
=====================================================
Runs after each scan to keep the public data repo fresh.
The dashboard reads from raw.githubusercontent.com/sumedhbasani/basani-data/main/

Usage:
    python3 push_public_data.py

Called automatically by GitHub Actions scanner workflow after each scan.
"""
import json, os, base64, urllib.request, urllib.error
from datetime import datetime

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_PAT", "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")
REPO  = "sumedhbasani/basani-data"
API   = "https://api.github.com"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_FILES = [
    "scan_output.json",
    "news.json",
    "unusual_whales.json",
    "calendar.json",
    "plays_log.json",
]


def gh(method, path, payload=None):
    url  = f"{API}/repos/{REPO}/{path}"
    hdrs = {
        "Authorization": f"token {TOKEN}",
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
    """Get current SHA of a file (needed to update it)."""
    data, status = gh("GET", f"contents/{remote_path}")
    if status == 200:
        return data.get("sha", "")
    return ""  # file doesn't exist yet


def push_file(local_name, remote_name=None):
    remote_name = remote_name or local_name
    local_path  = os.path.join(BASE_DIR, local_name)

    if not os.path.exists(local_path):
        print(f"  ⚠ {local_name} not found — skipping")
        return False

    with open(local_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()

    sha = get_sha(remote_name)

    payload = {
        "message": f"chore: update {remote_name} [{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC]",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha

    _, status = gh("PUT", f"contents/{remote_name}", payload)
    ok = status in (200, 201)
    print(f"  {'✅' if ok else '❌'} {remote_name}  (HTTP {status})")
    return ok


if __name__ == "__main__":
    print("=" * 56)
    print(f"  BASANI — pushing data to public repo")
    print(f"  {REPO}")
    print("=" * 56)

    ok_count = 0
    for fname in DATA_FILES:
        if push_file(fname):
            ok_count += 1

    print()
    print(f"  {ok_count}/{len(DATA_FILES)} files pushed")
    print(f"  Public data: https://raw.githubusercontent.com/{REPO}/main/")
    print("=" * 56)
