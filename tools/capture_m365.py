"""Capture M365 Copilot Chat network traffic for protocol reverse-engineering.

Opens a visible Chromium via Playwright, waits for you to sign in with your
work/school account, then records every HTTP request, WebSocket frame, and
Server-Sent Event stream while you send a couple of test messages.

Outputs two files under captures/:
  * m365_capture.json — full structured record (URLs, headers, bodies, WS frames)
  * m365_capture.md   — human-readable summary highlighting likely endpoints,
                        token audience/scopes, and streaming protocol shape

The persistent profile lives in session/profile/ so you only sign in once.
Values that look like secrets (Bearer tokens, cookies) are redacted in the
markdown summary but kept in the JSON (which stays local, git-ignored).

Usage:
    python -m playwright install chromium   # one-time
    python tools/capture_m365.py
"""

from __future__ import annotations

import base64
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright, Error as PlaywrightError


# ---- config ---------------------------------------------------------------

M365_URL = "https://m365.cloud.microsoft/chat"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = PROJECT_ROOT / "session" / "profile"
CAPTURE_DIR = PROJECT_ROOT / "captures"

# Endpoints we care about for the M365 Copilot chat protocol. Anything matching
# these host substrings is recorded verbatim (bodies included). Everything else
# is recorded as a header-only summary so the capture stays manageable.
INTERESTING_HOSTS = (
    "substrate.office.com",
    "m365.cloud.microsoft",
    "copilot.cloud.microsoft",
    "office.com/api",
    "outlook.office.com",
    "graph.microsoft.com",
    "loki.delve.office.com",
    "sydney.bing.com",  # legacy Copilot backend, in case M365 still uses it
)

# Header names to redact in the markdown summary. Case-insensitive.
REDACT_HEADERS = {"authorization", "cookie", "set-cookie", "x-anchormailbox"}

# Signed-in signal — several M365 apps store auth under different key shapes, so
# accept any of them. We check for:
#   * MSAL v2 account cache (`*account.keys`)
#   * Legacy MSAL / OWA auth blobs (any key with 'accesstoken' + 'credentialType')
#   * ADAL-style tokens (any key with 'adal.access.token')
# and fall back to a URL check (URL must be back on m365.cloud.microsoft, not on
# an AAD login domain). Returns a short diagnostic string when NOT signed in so
# the caller can surface why detection is stalling.
_SIGNED_IN_JS = """
() => {
  const diag = {url: location.href, msalAccountKeys: 0, tokensSeen: 0, adalSeen: 0};
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      const v = localStorage.getItem(k);
      if (!k) continue;
      if (k.indexOf('account.keys') !== -1) {
        try {
          const a = JSON.parse(v || 'null');
          const n = Array.isArray(a) ? a.length : (a ? Object.keys(a).length : 0);
          diag.msalAccountKeys += n;
        } catch (e) {}
      }
      if (v && v.indexOf('credentialType') !== -1
              && v.indexOf('AccessToken') !== -1) diag.tokensSeen++;
      if (k.indexOf('adal.access.token') !== -1) diag.adalSeen++;
    }
  } catch (e) {}
  const onLoginDomain = /login\\.microsoftonline\\.com|login\\.live\\.com/i.test(location.href);
  const onM365Domain = /m365\\.cloud\\.microsoft|office\\.com|copilot\\.cloud\\.microsoft/i.test(location.href);
  // Accept as soon as we're off the AAD login flow and back on an M365 host.
  // Token-cache presence is a nice-to-have but not required — some SSO paths
  // stash auth via cookies + non-standard storage keys, which the localStorage
  // sweep above misses.
  diag.onLoginDomain = onLoginDomain;
  diag.onM365Domain = onM365Domain;
  const signedIn = onM365Domain && !onLoginDomain;
  return {signedIn, diag};
}
"""

# Dump the full MSAL cache — every key that looks like a token/account/refresh
# entry. Used to figure out which scope/audience the chat call uses.
_DUMP_MSAL_JS = """
() => {
  const out = {};
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      const v = localStorage.getItem(k);
      if (!v) continue;
      if (v.indexOf('credentialType') !== -1
          || k.indexOf('msal') !== -1
          || k.indexOf('account') !== -1
          || v.indexOf('AccessToken') !== -1
          || v.indexOf('RefreshToken') !== -1) {
        out[k] = v;
      }
    }
  } catch (e) {}
  return out;
}
"""


# ---- helpers --------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_interesting(url: str) -> bool:
    lu = url.lower()
    return any(host in lu for host in INTERESTING_HOSTS)


def _redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    out = {}
    for k, v in headers.items():
        if k.lower() in REDACT_HEADERS:
            out[k] = f"<redacted; len={len(v)}>"
        else:
            out[k] = v
    return out


def _decode_jwt_payload(token: str) -> Optional[Dict[str, Any]]:
    """Return the payload dict of a JWT, or None if it doesn't look like one."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        pad = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        return json.loads(raw)
    except Exception:
        return None


def _summarize_token(auth_header: str) -> Optional[Dict[str, Any]]:
    """Pull aud/scp/appid/tid out of a Bearer JWT so we know what scope it is."""
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(" ", 1)[1]
    payload = _decode_jwt_payload(token)
    if not payload:
        return {"note": "not a JWT (opaque token?)", "prefix": token[:12]}
    return {
        "aud": payload.get("aud"),
        "scp": payload.get("scp"),
        "roles": payload.get("roles"),
        "appid": payload.get("appid"),
        "tid": payload.get("tid"),
        "upn": payload.get("upn") or payload.get("unique_name"),
        "exp": payload.get("exp"),
    }


# ---- capturer -------------------------------------------------------------


class Capturer:
    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []
        self.ws_records: List[Dict[str, Any]] = []
        self.token_samples: List[Dict[str, Any]] = []
        self._seen_tokens: set = set()

    # HTTP ------------------------------------------------------------------

    def on_request(self, request) -> None:
        if not _is_interesting(request.url):
            return
        headers = request.headers
        auth = headers.get("authorization") or headers.get("Authorization")
        token_summary = _summarize_token(auth) if auth else None
        if token_summary and auth:
            fingerprint = (
                token_summary.get("aud"),
                token_summary.get("scp"),
                token_summary.get("appid"),
            )
            if fingerprint not in self._seen_tokens:
                self._seen_tokens.add(fingerprint)
                self.token_samples.append({
                    "seen_at": _now_iso(),
                    "url": request.url,
                    **token_summary,
                })

        body = None
        try:
            body = request.post_data
        except PlaywrightError:
            pass

        self.records.append({
            "ts": _now_iso(),
            "phase": "request",
            "method": request.method,
            "url": request.url,
            "resource_type": request.resource_type,
            "headers": dict(headers),
            "post_data": body,
            "token_summary": token_summary,
        })

    def on_response(self, response) -> None:
        if not _is_interesting(response.url):
            return
        content_type = (response.headers.get("content-type") or "").lower()
        body_snippet: Optional[str] = None
        # Skip binary/large bodies. Only text-shaped responses give us protocol
        # hints (JSON, SSE, plain text).
        if any(t in content_type for t in ("json", "text", "event-stream", "xml")):
            try:
                text = response.text()
                body_snippet = text[:20000]  # cap per response to keep JSON sane
            except PlaywrightError:
                body_snippet = None

        self.records.append({
            "ts": _now_iso(),
            "phase": "response",
            "status": response.status,
            "url": response.url,
            "content_type": content_type,
            "headers": dict(response.headers),
            "body": body_snippet,
        })

    # WebSocket -------------------------------------------------------------

    def on_websocket(self, ws) -> None:
        # Record everything WS-shaped; M365 might not use WS at all, but if it
        # does, missing frames = missing protocol.
        entry: Dict[str, Any] = {
            "url": ws.url,
            "opened_at": _now_iso(),
            "frames": [],
            "closed_at": None,
        }

        def on_sent(payload):
            entry["frames"].append({
                "ts": _now_iso(),
                "dir": "send",
                "data": _decode_frame(payload),
            })

        def on_recv(payload):
            entry["frames"].append({
                "ts": _now_iso(),
                "dir": "recv",
                "data": _decode_frame(payload),
            })

        def on_close():
            entry["closed_at"] = _now_iso()

        try:
            ws.on("framesent", on_sent)
            ws.on("framereceived", on_recv)
            ws.on("close", lambda _=None: on_close())
        except PlaywrightError:
            pass
        self.ws_records.append(entry)

    # Output ----------------------------------------------------------------

    def dump(self, capture_dir: Path, msal_cache: Dict[str, str],
            cookies: List[Dict[str, str]]) -> None:
        capture_dir.mkdir(parents=True, exist_ok=True)
        json_path = capture_dir / "m365_capture.json"
        md_path = capture_dir / "m365_capture.md"
        chat_frames_path = capture_dir / "chat_frames.md"

        payload = {
            "captured_at": _now_iso(),
            "landing_url": M365_URL,
            "http_records": self.records,
            "ws_records": self.ws_records,
            "token_samples": self.token_samples,
            "msal_cache_keys": sorted(msal_cache.keys()),
            "msal_cache": msal_cache,  # full — file is git-ignored
            "cookie_names": sorted({c.get("name") for c in cookies if c.get("name")}),
        }
        json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

        md_path.write_text(self._render_markdown(payload), encoding="utf-8")

        # Untruncated chat-hub frames, pretty-printed. This is what the driver
        # implementation actually needs to work from — the SignalR message
        # protocol (handshake, invocation, update stream frames).
        chat_frames_path.write_text(self._render_chat_frames(payload), encoding="utf-8")

        print(f"\n[capture] wrote {json_path}")
        print(f"[capture] wrote {md_path}")
        print(f"[capture] wrote {chat_frames_path}")

    def _render_chat_frames(self, payload: Dict[str, Any]) -> str:
        """Emit every Chathub WS frame in full, pretty-printed where possible."""
        lines: List[str] = [f"# M365 Copilot Chathub frames — {payload['captured_at']}\n"]
        chat_sessions = [w for w in payload["ws_records"]
                         if "/m365Copilot/Chathub" in (w.get("url") or "")]
        if not chat_sessions:
            lines.append("_No Chathub sessions captured._\n")
            return "\n".join(lines)

        for idx, w in enumerate(chat_sessions, 1):
            base_url = (w["url"] or "").split("?", 1)[0]
            lines.append(f"## Session {idx} — `{base_url}`")
            lines.append(f"- opened: {w.get('opened_at')}")
            lines.append(f"- closed: {w.get('closed_at')}")
            lines.append(f"- frames: {len(w.get('frames') or [])}\n")

            for i, f in enumerate(w.get("frames") or []):
                data = f.get("data")
                # M365 Chathub uses SignalR JSON hub protocol with a 0x1E record
                # separator between frames. Split so each JSON object stands
                # alone; leaves non-SignalR frames untouched.
                parts = _split_signalr(data) if isinstance(data, str) else [data]
                for j, part in enumerate(parts):
                    label = f"### {f.get('dir')} · frame {i}"
                    if len(parts) > 1:
                        label += f" · sub {j}"
                    lines.append(label)
                    lines.append(f"_{f.get('ts')}_\n")
                    pretty = _pretty_json(part)
                    lines.append("```json")
                    lines.append(pretty)
                    lines.append("```\n")
        return "\n".join(lines)

    def _render_markdown(self, payload: Dict[str, Any]) -> str:
        lines: List[str] = []
        lines.append(f"# M365 Copilot capture — {payload['captured_at']}\n")
        lines.append(f"Landing URL: `{payload['landing_url']}`\n")

        # Token audiences
        lines.append("## Token samples (JWT payload aud/scp/appid)\n")
        if not payload["token_samples"]:
            lines.append("_No Bearer tokens seen. Chat may authenticate via cookies only._\n")
        for t in payload["token_samples"]:
            lines.append(f"- URL: `{t['url']}`")
            lines.append(f"  - aud: `{t.get('aud')}`")
            lines.append(f"  - scp: `{t.get('scp')}`")
            lines.append(f"  - roles: `{t.get('roles')}`")
            lines.append(f"  - appid: `{t.get('appid')}`")
            lines.append(f"  - tid: `{t.get('tid')}`")
            lines.append(f"  - upn: `{t.get('upn')}`\n")

        # HTTP endpoints
        lines.append("## Unique interesting endpoints (method + URL)\n")
        seen = set()
        for r in payload["http_records"]:
            if r.get("phase") != "request":
                continue
            key = (r["method"], r["url"].split("?", 1)[0])
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- `{r['method']}` `{r['url']}`")
        lines.append("")

        # Streaming shape
        stream_hits = [r for r in payload["http_records"]
                       if r.get("phase") == "response"
                       and "event-stream" in (r.get("content_type") or "")]
        lines.append("## SSE responses seen\n")
        if not stream_hits:
            lines.append("_No text/event-stream responses. Streaming may use WS or chunked JSON._\n")
        for r in stream_hits[:5]:
            lines.append(f"- `{r['url']}`")
            snippet = (r.get("body") or "")[:800].replace("\n", "\n    ")
            lines.append(f"  - first bytes:\n\n    ```\n    {snippet}\n    ```\n")

        # WS
        lines.append("## WebSocket sessions\n")
        if not payload["ws_records"]:
            lines.append("_None. M365 Copilot Chat is not using WebSocket for this session._\n")
        for w in payload["ws_records"]:
            lines.append(f"- URL: `{w['url']}`")
            lines.append(f"  - opened: {w['opened_at']}, closed: {w['closed_at']}")
            lines.append(f"  - frames: {len(w['frames'])}")
            for f in w["frames"][:6]:
                data = str(f["data"])[:200]
                lines.append(f"    - {f['dir']}: `{data}`")
            lines.append("")

        # MSAL keys
        lines.append("## MSAL localStorage keys\n")
        for k in payload["msal_cache_keys"]:
            lines.append(f"- `{k}`")
        lines.append("")

        # Cookie names
        lines.append("## Cookie names\n")
        for c in payload["cookie_names"]:
            lines.append(f"- `{c}`")

        return "\n".join(lines) + "\n"


def _decode_frame(payload: Any) -> Any:
    """Turn a WS frame payload into something JSON-serializable."""
    if isinstance(payload, (bytes, bytearray)):
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary; len={len(payload)}>"
    return payload


def _split_signalr(data: str) -> List[str]:
    """SignalR JSON-hub uses 0x1E as a record separator between frames.

    A single WebSocket packet often carries several JSON payloads glued together
    with 0x1E. Splitting on it turns them back into individual objects for
    per-frame pretty-printing. Frames without the separator pass through
    unchanged so non-SignalR sessions (e.g. Trouter) still render.
    """
    if "" not in data:
        return [data]
    return [p for p in data.split("") if p]


def _pretty_json(data: Any) -> str:
    """Pretty-print JSON if the payload parses, else return the raw string."""
    if not isinstance(data, str):
        try:
            return json.dumps(data, indent=2, ensure_ascii=False, default=str)
        except Exception:
            return repr(data)
    stripped = data.strip()
    if not stripped:
        return data
    # SignalR framing sometimes prepends a control byte or protocol prefix; try
    # to parse pure JSON first, then fall back to raw.
    try:
        return json.dumps(json.loads(stripped), indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        return data


# ---- driver ---------------------------------------------------------------


def _prompt(msg: str) -> None:
    print(f"\n>>> {msg}", flush=True)


def _wait_for_signin(page, timeout: int = 1800) -> bool:
    """Wait for the user to sign in, printing URL changes so progress is visible.

    Strategy: log every URL change in the browser window (every 3s), don't
    auto-terminate on any single URL — just wait until the user presses Enter
    in the terminal. That way there's no ambiguity: the user drives the login
    at their own pace, and every AAD redirect step is visible in the log so
    if something goes wrong we can see exactly where.
    """
    _prompt("A Chromium window has opened. Sign in there with your work account.")
    _prompt("The URL is printed here whenever it changes so you can see progress.")
    _prompt("Press Enter here ONLY AFTER you can see the Copilot Chat page and")
    _prompt("its message box is ready to type in.")
    _prompt("")

    import select as _select

    deadline = time.time() + timeout
    last_url = None
    last_tick = 0.0
    while time.time() < deadline:
        rlist, _, _ = _select.select([sys.stdin], [], [], 0)
        if rlist:
            sys.stdin.readline()
            try:
                current = page.url
            except PlaywrightError:
                current = "<unknown>"
            _prompt(f"Manual confirmation received. Current URL: {current}")
            return True

        try:
            url = page.url
        except PlaywrightError:
            _prompt("Browser window was closed. Aborting.")
            return False

        if url != last_url:
            _prompt(f"URL changed → {url}")
            last_url = url

        now = time.time()
        if now - last_tick >= 15:
            _prompt(f"…still waiting. Current URL: {url}. Press Enter when ready.")
            last_tick = now

        try:
            page.wait_for_timeout(1500)
        except PlaywrightError:
            _prompt("Browser window was closed. Aborting.")
            return False
    _prompt("Timed out waiting for sign-in confirmation.")
    return False


def _wait_for_enter(prompt: str) -> None:
    _prompt(prompt)
    try:
        sys.stdin.readline()
    except KeyboardInterrupt:
        raise


def main() -> int:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

    cap = Capturer()

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )

        # Wire up recorders on every page in the context.
        def wire(page):
            page.on("request", cap.on_request)
            page.on("response", cap.on_response)
            page.on("websocket", cap.on_websocket)

        for p in ctx.pages:
            wire(p)
        ctx.on("page", wire)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(60_000)

        _prompt(f"Opening {M365_URL}")
        page.goto(M365_URL, wait_until="domcontentloaded")

        if not _wait_for_signin(page):
            print("[capture] sign-in not detected within timeout; aborting.")
            ctx.close()
            return 1

        # Let the SPA settle after sign-in so the first chat request records cleanly.
        page.wait_for_timeout(4000)

        _wait_for_enter(
            "STEP 1 — In the Chromium window, TYPE a short message like 'hello' "
            "into the Copilot chat box and press ENTER inside the browser. "
            "Wait until the reply has FULLY finished streaming (no more tokens "
            "appearing, cursor stops blinking). THEN press Enter here."
        )
        _prompt(f"After message 1: recorded {len(cap.records)} HTTP records, "
                f"{len(cap.ws_records)} WS sessions.")

        _wait_for_enter(
            "STEP 2 — Send a SECOND message that touches your data "
            "(e.g. 'What meetings do I have today?'). Wait for the reply to "
            "fully finish streaming, then press Enter here."
        )
        _prompt(f"After message 2: recorded {len(cap.records)} HTTP records, "
                f"{len(cap.ws_records)} WS sessions.")

        # Small settle so any trailing analytics/pings land.
        page.wait_for_timeout(2000)

        # Snapshot MSAL cache + cookies before teardown.
        try:
            msal = page.evaluate(_DUMP_MSAL_JS) or {}
        except PlaywrightError:
            msal = {}
        try:
            cookies = ctx.cookies()
        except PlaywrightError:
            cookies = []

        ctx.close()

    cap.dump(CAPTURE_DIR, msal, cookies)
    print("\n[capture] done. Review captures/m365_capture.md, share JSON with Claude.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
