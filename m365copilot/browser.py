"""Playwright-backed sign-in and Sydney-token capture.

The M365 web SPA only mints a Sydney chat token when a chat turn actually opens
the Chathub WebSocket — so reading MSAL localStorage isn't enough by itself
(the entry doesn't exist until the SPA has loaded the /chat route and sent at
least one message). We solve that by instrumenting Playwright and *capturing
the token off the Chathub WebSocket URL* the SPA opens:

  wss://substrate.office.com/m365Copilot/Chathub/{oid}@{tid}?access_token=<JWT>&...

This is the same technique the consumer Copilot bridge uses for federated
Google logins where the MSAL cache is encrypted. Here it's not encryption but
*non-existence* until we drive a chat turn — either the user's own message
during sign-in, or an automated warm-up we send after sign-in.

Two entry points:
  * :meth:`login` — visible browser for the first-ever sign-in; auto-warms up
    to mint the token after sign-in completes.
  * :meth:`acquire_token` — headless read for a signed-in profile; also
    warm-up-driven so it works even when the token wasn't in MSAL yet.

The MSAL cache is still consulted as a fast path when it happens to hold a
matching token (rare in practice but free to check).
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright, Error as PlaywrightError

from .auth import DEFAULT_AUTH_FILE, DEFAULT_PROFILE_DIR, save_auth


M365_URL = "https://m365.cloud.microsoft/chat"

# The Sydney chat token is scoped to this audience. Every other token in the
# MSAL cache (Graph, Substrate search, arc.msn.com, ...) is wrong for us and
# the WS upgrade rejects it.
SYDNEY_AUD_PREFIX = "https://substrate.office.com/sydney"
SYDNEY_MSAL_KEY_MARKER = "https://substrate.office.com/sydney/.default"

# The Chathub URL fragment we listen for on the WebSocket layer.
CHATHUB_URL_MARKER = "/m365Copilot/Chathub"


# --- in-page JavaScript ----------------------------------------------------

# Walk MSAL v2 localStorage and return every AccessToken cache entry.
_DUMP_ACCESS_TOKENS_JS = """
() => {
  const out = [];
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    const v = localStorage.getItem(k);
    if (!v || v.indexOf('"credentialType":"AccessToken"') === -1) continue;
    try {
      const o = JSON.parse(v);
      if (o && o.secret) out.push({key: k, secret: o.secret, target: o.target || ''});
    } catch (e) {}
  }
  return out;
}
"""

# True as soon as MSAL has cached any account.
_SIGNED_IN_JS = """
() => {
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (!k || k.indexOf('account.keys') === -1) continue;
    try {
      const v = JSON.parse(localStorage.getItem(k) || 'null');
      if (Array.isArray(v) ? v.length > 0 : (v && Object.keys(v).length > 0))
        return true;
    } catch (e) {}
  }
  return false;
}
"""


# --- helpers ---------------------------------------------------------------


def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        pad = "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return {}


def _is_sydney_token(token: str) -> bool:
    aud = (_decode_jwt_payload(token) or {}).get("aud") or ""
    return aud.startswith(SYDNEY_AUD_PREFIX)


def _extract_identity(token: str) -> Dict[str, Any]:
    """Pull tenant + object id + upn from a Sydney JWT payload."""
    payload = _decode_jwt_payload(token)
    return {
        "tenant_id": payload.get("tid"),
        "object_id": payload.get("oid"),
        "upn": payload.get("upn") or payload.get("unique_name"),
        "name": payload.get("name"),
        "expires_on": payload.get("exp"),
    }


def _pick_sydney_from_msal(tokens: list) -> Optional[str]:
    """Return the Sydney access-token secret from an MSAL AccessToken dump."""
    # Fast path: the cache key contains the scope marker.
    for t in tokens:
        if SYDNEY_MSAL_KEY_MARKER in (t.get("key") or ""):
            return t.get("secret")
    # Fallback: decode each JWT and match aud.
    for t in tokens:
        secret = t.get("secret") or ""
        if _is_sydney_token(secret):
            return secret
    return None


# --- browser wrapper -------------------------------------------------------


class BrowserAuth:
    """Persistent Playwright profile that yields Sydney chat tokens.

    Two entry points:
      * :meth:`acquire_token` — headless read for a signed-in profile. Loads
        the chat route, auto-sends a warm-up if needed, captures the token off
        the Chathub WebSocket URL.
      * :meth:`login` — visible sign-in for the first-ever run. Same warm-up
        flow after sign-in.
    """

    def __init__(
        self,
        profile_dir: str = DEFAULT_PROFILE_DIR,
        headless: bool = True,
        nav_timeout: int = 60,
    ):
        self.profile_dir = str(Path(profile_dir).resolve())
        self.headless = headless
        self.nav_timeout = nav_timeout

        self._pw = None
        self._context = None
        self._page = None
        self._captured_token: Optional[str] = None
        self._ws_listener_installed = False

    # -- lifecycle ----------------------------------------------------------

    def _start(self) -> None:
        if self._context is not None:
            return
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        # When visible-but-hidden mode is active, launch the window at the
        # smallest size Chrome allows and dump it off-screen so the user
        # doesn't see it. This is the trick that lets Entra silent SO go
        # through (headless mode is rejected by conditional-access policy)
        # while keeping the experience truly background.
        launch_args = ["--disable-blink-features=AutomationControlled",
                    "--no-proxy-server"]
        if not self.headless:
            launch_args += [
                "--window-size=1,1",
                "--window-position=-32000,-32000",
                "--disable-popup-blocking",
            ]
        self._context = self._pw.chromium.launch_persistent_context(
            self.profile_dir,
            headless=self.headless,
            args=launch_args,
            ignore_default_args=["--enable-automation"],
        )
        # Instrument BEFORE navigating so the first Chathub WS is caught.
        self._context.on("page", self._install_page_listeners)
        for p in self._context.pages:
            self._install_page_listeners(p)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._install_page_listeners(self._page)
        self._page.set_default_timeout(self.nav_timeout * 1000)
        self._page.goto(M365_URL, wait_until="domcontentloaded")

    def close(self) -> None:
        for attr, closer in (
            ("_context", lambda c: c.close()),
            ("_pw", lambda p: p.stop()),
        ):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    closer(obj)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._page = None

    def __enter__(self):
        self._start()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- listeners ----------------------------------------------------------

    def _install_page_listeners(self, page) -> None:
        """Capture the Sydney token off request Authorization headers and WS URLs.

        Two capture channels because M365 mints the token slightly differently
        depending on entry point:
          * request Authorization headers — for Substrate REST calls the SPA
            makes on the /chat route before the WS opens.
          * WS URL access_token= param — for the Chathub upgrade itself.
        Either one is enough; whichever fires first wins.
        """
        try:
            page.on("request", self._on_request)
            page.on("websocket", self._on_websocket)
        except PlaywrightError:
            pass

    def _on_request(self, request) -> None:
        if self._captured_token:
            return
        url = (request.url or "").lower()
        if "substrate.office.com" not in url:
            return
        auth = request.headers.get("authorization") or request.headers.get("Authorization")
        if not auth or not auth.lower().startswith("bearer "):
            return
        token = auth.split(" ", 1)[1]
        if _is_sydney_token(token):
            self._captured_token = token

    def _on_websocket(self, ws) -> None:
        try:
            url = ws.url
        except Exception:
            return
        if CHATHUB_URL_MARKER not in url:
            return
        if "access_token=" not in url:
            return
        try:
            token = (parse_qs(urlparse(url).query).get("access_token") or [None])[0]
        except Exception:
            token = None
        if token:
            self._captured_token = token

    # -- state readers ------------------------------------------------------

    def _signed_in(self) -> bool:
        assert self._page is not None
        try:
            return bool(self._page.evaluate(_SIGNED_IN_JS))
        except PlaywrightError:
            return False

    def _read_msal_sydney(self) -> Optional[str]:
        """Fast path — read Sydney token from MSAL cache when present."""
        assert self._page is not None
        try:
            tokens = self._page.evaluate(_DUMP_ACCESS_TOKENS_JS) or []
        except PlaywrightError:
            return None
        return _pick_sydney_from_msal(tokens)

    def _current_token(self) -> Optional[str]:
        """Return the freshest Sydney token we've seen from any source."""
        return self._captured_token or self._read_msal_sydney()

    # -- warm-up ------------------------------------------------------------

    def _send_warmup(self, text: str = "hi") -> bool:
        """Type a message into the Copilot composer and hit Enter.

        This triggers the SPA to open the Chathub WebSocket, which is what
        actually mints and reveals the Sydney token. Returns True if we found
        a composer and typed into it. Uses short per-selector timeouts so the
        caller (``acquire_token``'s retry loop) can back off quickly when the
        SPA hasn't finished hydrating.
        """
        assert self._page is not None
        selectors = (
            "div[contenteditable='true']",
            "[role='textbox']",
            "textarea",
        )
        for sel in selectors:
            try:
                self._page.wait_for_selector(sel, state="visible", timeout=1500)
            except PlaywrightError:
                continue
            try:
                self._page.click(sel)
                self._page.keyboard.type(text, delay=15)
                self._page.keyboard.press("Enter")
                return True
            except PlaywrightError:
                continue
        return False

    def _wait_for_token(self, timeout: int) -> Optional[str]:
        """Poll until a Sydney token appears from any capture channel."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            tok = self._current_token()
            if tok:
                return tok
            try:
                self._page.wait_for_timeout(500)
            except PlaywrightError:
                return None
        return None

    # -- public API ---------------------------------------------------------

    def acquire_token(self, timeout: int = 120, hidden: bool = True) -> Optional[Dict[str, Any]]:
        """Return current Sydney auth for a signed-in profile, or None.

        Loads the M365 Copilot SPA in a **visible-but-hidden** window and
        waits for one of three token sources:

          * A Chathub WebSocket URL (``access_token=`` param) — captured
            live when the SPA opens the chat socket.
          * An HTTP request bearing a Sydney-scoped ``Authorization: Bearer``
            header — captured from Substrate REST calls the SPA makes on
            hydration.
          * MSAL localStorage — read after the SPA has had time to hydrate.

        Why visible-but-hidden rather than headless? Some corporate Entra
        conditional-access policy rejects silent sign-on from Playwright's
        headless Chromium (the UA / sec-chua header signature differs from a
        normal user browser). When ``hidden=True`` we launch a real window
        but shrink it to 1x1px and move it off-screen so the user doesn't see
        it, while the page gets a normal-browser JS environment where
        Entra's silent SO completes normally and MSAL.js decrypts its
        cache on its own.
        """
        # Close any prior context so we can switch headless modes.
        self.close()
        self.headless = not hidden
        self._start()

        # Shrink the window so it's not visible on the user's screen. This
        # only affects the OS window chrome, not the page viewport.
        if hidden:
            try:
                # These Chrome switches redraw the window at 1x1 off-screen.
                # The page JS environment stays intact; silent SO goes through.
                self._context.add_init_script(
                    "Object.defineProperty(screen, 'availWidth', {get:() => 1});"
                )
            except PlaywrightError:
                pass

        # The SPA will redirect through login.microsoftonline.com and back.
        # For a signed-in profile this is a silent SO that completes in
        # 3-15 s. Wait for the chain to land back on m365.cloud.microsoft.
        deadline = time.time() + timeout
        m365_landed = False

        while time.time() < deadline:
            if self._page is None or self._page.is_closed():
                return None
            try:
                url = self._page.url
            except PlaywrightError:
                break

            if ("m365.cloud.microsoft" in url and "chat" in url
                    and "login" not in url):
                m365_landed = True
                break

            try:
                self._page.wait_for_timeout(1000)
            except PlaywrightError:
                break

        if not m365_landed:
            return None

        # Give the SPA time to hydrate and publish tokens.
        try:
            self._page.wait_for_timeout(5000)
        except PlaywrightError:
            return None

        # Fast path — captured from WS or REST traffic during load.
        tok = self._current_token()
        if tok:
            return self._snapshot(tok)

        # Slow path: drive a warm-up so the SPA opens the Chathub WS.
        if not self._send_warmup():
            return None

        warmup_deadline = time.time() + min(60, timeout)
        while time.time() < warmup_deadline:
            tok = self._current_token()
            if tok:
                return self._snapshot(tok)
            try:
                self._page.wait_for_timeout(500)
            except PlaywrightError:
                return None

        return None

    def login(self, path: str = DEFAULT_AUTH_FILE, timeout: int = 900) -> Dict[str, Any]:
        """Open a visible browser for interactive M365 sign-in, then snapshot.

        Opens a fresh persistent Chromium window (no initial navigation) so
        the user can sign in naturally — including MFA / phone-push / device
        compliance prompts. The script watches the page URL; once it lands
        back on ``m365.cloud.microsoft/chat`` it attempts a warm-up turn to
        mint the Sydney token.

        No terminal Enter needed — detection is automatic.
        """
        self.close()
        self.headless = False
        self._start()

        print(
            "\nA browser window is open at m365.cloud.microsoft/chat.\n"
            "Sign in with your work M365 account (MFA, conditional-access, etc.).\n"
            "This finishes by itself once sign-in is detected — no Enter needed.\n"
        )

        # Wait for sign-in (MSAL cache detects a cached account).
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._page is None or self._page.is_closed():
                print("[m365copilot] Browser window closed before sign-in completed.")
                return {}
            if self._signed_in():
                break
            self._page.wait_for_timeout(1500)
        else:
            print("[m365copilot] Sign-in not detected within timeout.")
            return {}

        # Fast path — already captured (unlikely on a fresh profile).
        tok = self._current_token()

        # Automated warm-up: navigate to /chat and try to type "hi".
        if not tok:
            print("[m365copilot] Signed in — trying automated warm-up...")
            self._navigate_to_chat()
            if self._send_warmup():
                tok = self._wait_for_token(30)

        # Manual fallback: ask the user to send one message themselves.
        if not tok:
            print(
                "\n[m365copilot] Automated warm-up didn't work — please send one\n"
                "  message yourself in the Copilot Chat window (e.g. 'hi').\n"
                "  The token capture happens automatically; no Enter needed.\n"
            )
            tok = self._wait_for_token(600)

        if not tok:
            print(
                "[m365copilot] No Sydney token captured. If you sent a message and "
                "got a reply but this still fails, please share the terminal output "
                "so we can inspect what the WebSocket layer saw."
            )
            self.close()
            return {}

        auth = self._snapshot(tok)
        save_auth(auth, path=path)
        print(f"\n[m365copilot] Auth saved to {path} (upn={auth.get('upn')})")
        self.close()
        return auth

    # -- internals ----------------------------------------------------------

    def _navigate_to_chat(self) -> None:
        """Make sure the page is on /chat, so the composer is available."""
        assert self._page is not None
        try:
            if "/chat" not in (self._page.url or ""):
                self._page.goto(M365_URL, wait_until="domcontentloaded")
                self._page.wait_for_timeout(2000)
        except PlaywrightError:
            pass

    def _snapshot(self, token: str) -> Dict[str, Any]:
        return {
            "access_token": token,
            "saved_at": time.time(),
            **_extract_identity(token),
        }

    @staticmethod
    def save(auth: Dict[str, Any], path: str = DEFAULT_AUTH_FILE) -> None:
        save_auth(auth, path=path)
