"""Persistent M365 Copilot auth cache.

Bridges the interactive Playwright sign-in (see :mod:`m365copilot.browser`) to
the headless SignalR driver: keeps a short-lived snapshot of the Sydney access
token + identity claims on disk, and transparently refreshes it from the
browser profile when it goes stale.

The Sydney JWT lives ~60-75 minutes. We refresh at 50 minutes to stay safely
inside its expiry window without pinging AAD on every request.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Optional

# All session state (browser profile + cached auth) lives under one folder,
# resolved relative to the project root (parent of this file's package).
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
SESSION_DIR = PACKAGE_ROOT / "session"
DEFAULT_PROFILE_DIR = str(SESSION_DIR / "profile")
DEFAULT_AUTH_FILE = str(SESSION_DIR / "token.json")

# Refresh well before the ~60-75 min Sydney token expiry.
AUTH_MAX_AGE = 50 * 60


def _decode_jwt_payload(token: str) -> dict:
    """Return the payload dict of a JWT, or {} if it doesn't parse."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        pad = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        return json.loads(raw)
    except Exception:
        return {}


def _token_expired(auth: dict, max_age: int) -> bool:
    """True if the cached token is past its safe-use window.

    Two checks: our own soft cap (max_age since saved_at) AND the JWT's own
    exp claim minus a 2-min buffer. Whichever fires first wins.
    """
    saved_at = auth.get("saved_at", 0)
    if time.time() - saved_at >= max_age:
        return True

    token = auth.get("access_token") or ""
    exp = _decode_jwt_payload(token).get("exp")
    if isinstance(exp, (int, float)) and time.time() >= exp - 120:
        return True
    return False


def load_auth(
    path: str = DEFAULT_AUTH_FILE,
    profile_dir: str = DEFAULT_PROFILE_DIR,
    max_age: int = AUTH_MAX_AGE,
    auto_login: bool = True,
) -> dict:
    """Return ``{access_token, tenant_id, object_id, upn, saved_at, ...}``.

    Three-tier refresh strategy:
      1. Cached token at ``path`` — return immediately if fresh.
      2. Silent refresh (no browser) — read the Entra refresh token from the
         Chromium profile and exchange it for a new Sydney access token via
         a pure HTTP POST to ``login.microsoftonline.com/.../oauth2/v2.0/token``.
         This is the same channel MSAL v2 uses internally; it's silent, fast,
         and works even when the Playwright SPA warm-up doesn't.
      3. (Fallback) Interactive browser — only when both (1) and (2) fail.
         This is the ``copilot login`` path.
    """
    p = Path(path)
    if p.exists():
        try:
            cached = json.loads(p.read_text(encoding="utf-8"))
            if cached.get("access_token") and not _token_expired(cached, max_age):
                return cached
        except (ValueError, OSError):
            pass  # corrupt/unreadable -> refresh below

    # Tier 2: silent refresh via Entra refresh-token exchange.
    # We need a tenant_id to know which Entra endpoint to hit. Borrow it from
    # the stale token.json if it exists, or fall back to the one from the
    # cached (now-expired) JWT payload.
    tenant_id = None
    if p.exists():
        try:
            cached = json.loads(p.read_text(encoding="utf-8"))
            tenant_id = cached.get("tenant_id")
        except Exception:
            pass

    from .silent_refresh import try_silent_refresh

    auth = try_silent_refresh(profile_dir, tenant_id)
    if auth and auth.get("access_token"):
        save_auth(auth, path=path)
        return auth

    # Tier 3: interactive browser (only when auto_login is allowed).
    from .browser import BrowserAuth

    bot = BrowserAuth(profile_dir=profile_dir, headless=True)
    try:
        auth = bot.acquire_token()
        if auth and auth.get("access_token"):
            bot.save(auth, path=path)
            return auth
    finally:
        bot.close()

    if not auto_login:
        raise RuntimeError(
            "Not signed in (no Sydney token in the browser profile). "
            "Run `python -m m365copilot login` and sign in first."
        )

    # First-time use or expired refresh token: sign in interactively.
    print("[m365copilot] No cached Copilot session — opening a browser to sign in...")
    auth = BrowserAuth(profile_dir=profile_dir, headless=False).login(path=path)
    if not auth.get("access_token"):
        raise RuntimeError(
            "Sign-in did not yield a Sydney token. Retry `python -m m365copilot login`."
        )
    return auth


def save_auth(auth: dict, path: str = DEFAULT_AUTH_FILE) -> None:
    """Write an auth dict to disk atomically."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps(auth, indent=2), encoding="utf-8")
    tmp.replace(dest)
