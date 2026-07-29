#!/usr/bin/env python3
"""
BASANI - Unusual Whales HTTP helper.

Single source of truth for UW API calls. All scanner/feed scripts should
import uw_get_json from this module instead of hand-rolling auth headers.

Why this exists:
  UW sits behind Cloudflare and rejects plain "Bearer <token>" with HTTP 403
  Error 1010 because the request lacks browser-fingerprint headers.
  Required header set:
    Authorization: Bearer <TOKEN>
    UW-CLIENT-API-ID: 100001
    User-Agent matching a modern browser
    Origin: https://unusualwhales.com
    Referer: https://unusualwhales.com/

Token resolution order:
    1. process env UW_TOKEN
    2. process env UNUSUAL_WHALES_API_KEY
    3. .env file in same directory as this script
"""
import os
import json
import gzip
import urllib.request
import urllib.error
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
BASE_URL = "https://api.unusualwhales.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

ENV_VAR_NAMES = ("UW_TOKEN", "UNUSUAL_WHALES_API_KEY")


def _strip(v):
    return v.strip().strip('"').strip("'") if v else ""


def _load_token() -> str:
    for k in ENV_VAR_NAMES:
        v = os.environ.get(k, "")
        if v:
            return _strip(v)
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        try:
            with env_file.open() as f:
                for line in f:
                    if "=" not in line:
                        continue
                    k = line.split("=", 1)[0].strip()
                    if k in ENV_VAR_NAMES:
                        return _strip(line.split("=", 1)[1])
        except Exception:
            pass
    return ""


def _headers(token: str) -> dict:
    return {
        "Authorization": "Bearer " + token,
        "UW-CLIENT-API-ID": "100001",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": UA,
        "Origin": "https://unusualwhales.com",
        "Referer": "https://unusualwhales.com/",
    }


def _decode(body: bytes, content_encoding: str | None) -> bytes:
    if content_encoding == "gzip":
        return gzip.decompress(body)
    return body


def uw_get_json(path: str, params: dict | None = None, timeout: int = 15):
    """
    GET https://api.unusualwhales.com/<path>?<params>
    Returns (data, status_code).

    On 401/403 - token missing or wrong.
    On 404     - endpoint doesn't exist for this plan tier.
    On 429     - rate limited; caller should back off.
    """
    tok = _load_token()
    if not tok:
        return {"error": "UW_TOKEN missing"}, 0

    url = BASE_URL + path
    if params:
        qs = "&".join(
            f"{k}={v}" for k, v in params.items() if v is not None
        )
        if qs:
            url += "?" + qs

    req = urllib.request.Request(url, headers=_headers(tok))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = _decode(r.read(), r.headers.get("Content-Encoding"))
            try:
                return json.loads(body), r.status
            except Exception:
                return {"raw": body.decode(errors="replace")[:500]}, r.status
    except urllib.error.HTTPError as e:
        try:
            body = _decode(e.read(), e.headers.get("Content-Encoding"))
            return (json.loads(body) if body else {}), e.code
        except Exception:
            return {"error": f"HTTP {e.code}"}, e.code
    except Exception as e:
        return {"error": str(e)}, 0


# Backwards-compatible alias used by older modules.
def uw_get(path: str, params: dict | None = None, timeout: int = 15):
    return uw_get_json(path, params, timeout)


if __name__ == "__main__":
    tok = _load_token()
    print("Token len:", len(tok))
    if not tok:
        print("No UW_TOKEN found")
    else:
        for ep in (
            "/api/market/market-tide",
            "/api/stock/NVDA/info",
            "/api/darkpool/recent",
            "/api/option-trades/flow-alerts?limit=5",
            "/api/congress/recent-trades?limit=5",
            "/api/news/headlines?limit=5",
            "/api/market/economic-calendar",
        ):
            d, code = uw_get_json(ep)
            if isinstance(d, dict):
                items = d.get("data", [])
                count = len(items) if isinstance(items, list) else "?"
            else:
                count = len(d) if hasattr(d, "__len__") else "?"
            print(f"  {code}  {str(count):>4} items  {ep}")
