"""
BASANI — Supabase Database Client
==================================
All writes go through this module.
Frontend reads directly from Supabase REST API using the anon key.

Table: basani_data(key TEXT PK, payload JSONB, refreshed_at TIMESTAMPTZ)

Keys written by the backend:
  scan      ← scan_output.json      (Alpaca prices + signals)
  news      ← news.json             (Benzinga + X API)
  whales    ← unusual_whales.json   (Unusual Whales flow)
  calendar  ← calendar.json         (earnings + economic events)
  massive   ← massive_data.json     (Massive/Polygon raw data)
  health    ← health summary        (source status + freshness)
  plays     ← plays_log.json        (tracked plays)
"""

import json
import logging
import os
from datetime import datetime, timezone

import httpx

log = logging.getLogger("basani.db")

_SUPABASE_ANON = "sb_publishable_dy77qQrBEFQjC-fQLgHcyA_6iX3krzo"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://lsvbhlgxeddssgdpvwtq.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", _SUPABASE_ANON)

# Timeout for all Supabase calls
_TIMEOUT = 15


def _headers() -> dict:
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }


def _configured() -> bool:
    if not SUPABASE_URL or not SUPABASE_KEY:
        log.warning("Supabase not configured (SUPABASE_URL or SUPABASE_SERVICE_KEY missing)")
        return False
    return True


def upsert(key: str, payload) -> bool:
    """
    Upsert a JSON payload into basani_data.
    Returns True on success, False on any failure.
    Never raises — failures are logged and silently skipped.
    Cached data in Supabase is preserved on failure.
    """
    if not _configured():
        return False
    try:
        if isinstance(payload, str):
            payload = json.loads(payload)

        row = {
            "key":          key,
            "payload":      payload,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }

        resp = httpx.post(
            f"{SUPABASE_URL}/rest/v1/basani_data",
            headers=_headers(),
            json=row,
            timeout=_TIMEOUT,
        )

        if resp.status_code in (200, 201):
            log.info("db.upsert(%s) ✓", key)
            return True

        # Some Supabase versions return 204 on upsert
        if resp.status_code == 204:
            log.info("db.upsert(%s) ✓ (204)", key)
            return True

        log.error("db.upsert(%s) failed %d: %s", key, resp.status_code, resp.text[:300])
        return False

    except httpx.TimeoutException:
        log.error("db.upsert(%s) timeout after %ds", key, _TIMEOUT)
        return False
    except Exception as exc:
        log.exception("db.upsert(%s) exception: %s", key, exc)
        return False


def get(key: str) -> dict | None:
    """Read one row back by key. Returns None on any error."""
    if not _configured():
        return None
    try:
        resp = httpx.get(
            f"{SUPABASE_URL}/rest/v1/basani_data",
            headers={**_headers(), "Prefer": ""},
            params={"key": f"eq.{key}", "select": "key,payload,refreshed_at"},
            timeout=_TIMEOUT,
        )
        rows = resp.json()
        return rows[0] if rows else None
    except Exception as exc:
        log.error("db.get(%s) failed: %s", key, exc)
        return None


def get_all_keys() -> dict:
    """Return {key: refreshed_at} for all stored keys. Used by /status endpoint."""
    if not _configured():
        return {}
    try:
        resp = httpx.get(
            f"{SUPABASE_URL}/rest/v1/basani_data",
            headers={**_headers(), "Prefer": ""},
            params={"select": "key,refreshed_at"},
            timeout=_TIMEOUT,
        )
        return {r["key"]: r["refreshed_at"] for r in (resp.json() or [])}
    except Exception as exc:
        log.error("db.get_all_keys() failed: %s", exc)
        return {}
