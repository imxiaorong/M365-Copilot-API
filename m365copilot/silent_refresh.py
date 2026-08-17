"""Silent Sydney token refresh — pure HTTP, no browser window.

Reads the Entra refresh token from the MSAL cache in the Chromium profile
(accessible via Playwright headless, no visible window), then POSTs to the
Entra /token endpoint to exchange it for a fresh Sydney access token.

This is the same flow MSAL v2 runs internally — we just drive it ourselves
so we can avoid the Playwright warm-up dance entirely.

Called from :func:`load_auth` when the cached Sydney token is stale.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

# Refresh token + client_id + scope for the Substrate Sydney resource.
# The client_id below is the Office.com first-party app (same as the web
# SPA uses). tenant_id comes from the JWT stored in token.json.
OFFICE_CLIENT_ID = "c0ab8ce9-e9a0-42e7-b064-33d422df41f1"
REFRESH_TOKEN_KEY = "refreshtoken|4765445b-32c6-49b0-83e6-1d93765276ca|||"


def _get_refresh_token(profile_dir: str) -> Optional[str]:
    """Read the Entra refresh token from the Chromium LevelDB on disk.

    The MSAL v2 cache for ``m365.cloud.microsoft`` is persisted in
    ``<profile>/Default/Local Storage/leveldb/`` as a LevelDB key-value store.
    We dump the relevant key with ``strings`` (the data is small and ASCII)
    and parse the JSON value inline.

    This avoids any Playwright browser — no window, no redirect, no JS.
    """
    ldb = Path(profile_dir) / "Default" / "Local Storage" / "leveldb"
    if not ldb.is_dir():
        return None

    import subprocess

    # Read the value(s) associated with the refresh-token key for the
    # Office.com first-party client (4765445b-...).
    try:
        result = subprocess.run(
            ["strings", "*.ldb", "*.log"],
            capture_output=True, text=True, timeout=10,
            cwd=str(ldb),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    for line in (result.stdout or "").splitlines():
        # Look for the MSAL refrestoken entry for client 4765445b.
        # The line is the raw JSON value that localStorage stores under that key.
        if "refreshtoken|4765445b" not in line:
            continue
        # Skip the key itself (lines without a JSON value).
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            secret = obj.get("secret")
            if secret:
                return secret
        except (json.JSONDecodeError, KeyError, TypeError):
            continue

    return None


def _exchange_refresh_token(
    refresh_token: str,
    tenant_id: str,
    scope: str = "https://substrate.office.com/sydney/.default",
) -> Optional[Dict[str, Any]]:
    """POST to Entra /token endpoint to exchange a refresh token for a new
    Sydney access token.

    Returns the full token response dict on success, or None on failure.
    """
    import urllib.request
    from urllib.parse import urlencode

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": OFFICE_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": scope,
    }

    req = urllib.request.Request(url, data=urlencode(data).encode(), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("client-request-id", str(uuid.uuid4()))
    req.add_header("return-client-request-id", "true")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            return body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        print(f"[silent_refresh] HTTP {exc.code}: {body[:200]}")
        return None
    except Exception as exc:
        print(f"[silent_refresh] network error: {exc}")
        return None


def try_silent_refresh(profile_dir: str, tenant_id: str) -> Optional[Dict[str, Any]]:
    """Attempt a silent refresh: read refresh token from profile, exchange
    for a new Sydney access token.

    Returns a dict matching the ``load_auth`` shape (access_token, saved_at,
    tenant_id, object_id, upn) on success, or None on failure.
    """
    if not tenant_id:
        return None

    print("[silent_refresh] reading refresh token from profile...")
    rt = _get_refresh_token(profile_dir)
    if not rt:
        print("[silent_refresh] no refresh token found in profile")
        return None

    print("[silent_refresh] exchanging for new Sydney token...")
    resp = _exchange_refresh_token(rt, tenant_id)
    if not resp or not resp.get("access_token"):
        print("[silent_refresh] token exchange failed")
        return None

    token = resp["access_token"]
    # Decode JWT to extract identity claims.
    parts = token.split(".")
    pad = "=" * (-len(parts[1]) % 4)
    import base64
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))

    result = {
        "access_token": token,
        "saved_at": time.time(),
        "tenant_id": payload.get("tid") or tenant_id,
        "object_id": payload.get("oid"),
        "upn": payload.get("upn") or payload.get("unique_name"),
        "name": payload.get("name"),
    }
    print(f"[silent_refresh] success — upn={result.get('upn')}")
    return result