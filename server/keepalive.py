"""Background task that keeps the Sydney access token fresh via silent refresh.

Sydney tokens live ~60-75 minutes. This task refreshes them headlessly every
40 minutes using the visible-but-hidden Playwright strategy (window 1x1,
positioned off-screen) so McDonald's Entra conditional-access allows the
silent SO to complete. The user sees no window.

The refresh shares the same ``_get_upstream_lock()`` as the request handlers
so that a keepalive refresh and a user request never race on the same
Playwright profile.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Optional


REFRESH_INTERVAL_SECONDS = 55 * 60      # 55 min tick — reduced from 40min
                                         # to avoid hitting Entra CA policy
                                         # (SMS MFA) on every refresh.
REFRESH_AGE_THRESHOLD = 50 * 60         # refresh when token is >= 50 min old


def _log(msg: str) -> None:
    print(f"[keepalive] {msg}", flush=True)


async def _refresh_once() -> None:
    """Run one refresh attempt, serialised behind the upstream lock so it
    never races with a user request on the same Playwright profile."""
    from m365copilot.auth import DEFAULT_AUTH_FILE, DEFAULT_PROFILE_DIR, save_auth
    from m365copilot.browser import BrowserAuth
    from .lock import get_upstream_lock

    # Before launching a browser, check if the current JWT itself is still
    # valid. If it has > 5 min left, skip the refresh entirely — the token
    # will be reused by the next request. This cuts the number of Playwright
    # launches by ~50% (one per token lifetime instead of continuous).
    if _jwt_has_life(DEFAULT_AUTH_FILE, min_life_seconds=300):
        return

    def _do_refresh() -> Optional[dict]:
        bot = BrowserAuth(profile_dir=DEFAULT_PROFILE_DIR, headless=True)
        try:
            return bot.acquire_token(timeout=120, hidden=True)
        finally:
            bot.close()

    async with get_upstream_lock():
        auth = await asyncio.to_thread(_do_refresh)
    if auth and auth.get("access_token"):
        save_auth(auth, path=DEFAULT_AUTH_FILE)
        _log(f"refreshed Sydney token (upn={auth.get('upn')})")
    else:
        _log("refresh failed (Entra refresh token may be expired or CA blocked); "
             "next request will try the interactive path")


async def keepalive_loop() -> None:
    """Long-running task: refresh Sydney token every 40 minutes.

    Started in the FastAPI startup hook. Sleeps first so we don't do work at
    boot when token.json is already fresh from a recent ``copilot login``.
    """
    from m365copilot.auth import DEFAULT_AUTH_FILE

    auth_path = Path(DEFAULT_AUTH_FILE)
    _log(f"keep-alive scheduler started (every {REFRESH_INTERVAL_SECONDS // 60} min)")

    while True:
        try:
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
            age = _token_age_seconds(auth_path)
            if age is None:
                _log("no token.json yet — skipping (run `copilot login` first)")
                continue
            if age < REFRESH_AGE_THRESHOLD:
                _log(f"token is {int(age)}s old (< {REFRESH_AGE_THRESHOLD}s), skipping")
                continue
            _log(f"token is {int(age)}s old — refreshing headlessly")
            await _refresh_once()
        except asyncio.CancelledError:
            _log("keep-alive scheduler cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 — never let the task die
            _log(f"tick error: {exc!r} — will retry on the next interval")


def _token_age_seconds(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
        saved_at = float(auth.get("saved_at") or 0)
        if saved_at <= 0:
            return None
        return time.time() - saved_at
    except Exception:
        return None


def _jwt_has_life(path: str, min_life_seconds: int = 300) -> bool:
    """Return True if the cached JWT has at least ``min_life_seconds`` left.

    Reads the JWT's ``exp`` claim without decoding the full payload. Avoids
    launching a Playwright browser when the current token is still fresh.
    """
    import base64
    try:
        p = Path(path)
        if not p.exists():
            return False
        auth = json.loads(p.read_text(encoding="utf-8"))
        token = auth.get("access_token", "")
        if not token:
            return False
        # Parse the JWT payload (second segment).
        parts = token.split(".")
        if len(parts) < 2:
            return False
        pad = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return False
        remaining = exp - time.time()
        return remaining >= min_life_seconds
    except Exception:
        return False