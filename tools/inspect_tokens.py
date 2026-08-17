"""Open the persistent profile, dump every AccessToken entry, and pretty-print.

Purpose: figure out what the actual MSAL cache-key format is for the Sydney
token on this specific tenant. The browser.py picker is too strict; use this
to learn the real shape and fix the picker.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = PROJECT_ROOT / "session" / "profile"
M365_URL = "https://m365.cloud.microsoft/chat"


_DUMP_ALL_JS = """
() => {
  const out = [];
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    const v = localStorage.getItem(k);
    if (!v) continue;
    let credType = null;
    let target = null;
    let realm = null;
    let clientId = null;
    let expiresOn = null;
    let tokenPrefix = null;
    if (v.indexOf('credentialType') !== -1) {
      try {
        const o = JSON.parse(v);
        credType = o.credentialType || null;
        target = o.target || null;
        realm = o.realm || null;
        clientId = o.clientId || null;
        expiresOn = o.expiresOn || null;
        if (o.secret) tokenPrefix = o.secret.substring(0, 20) + '...';
      } catch (e) {}
    }
    out.push({key: k, credentialType: credType, target: target,
              realm: realm, clientId: clientId, expiresOn: expiresOn,
              tokenPrefix: tokenPrefix, valueLen: v.length});
  }
  return out;
}
"""


def _decode_jwt(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        pad = "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return {}


_DUMP_SECRETS_JS = """
() => {
  const out = {};
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    const v = localStorage.getItem(k);
    if (!v) continue;
    if (v.indexOf('"credentialType":"AccessToken"') === -1) continue;
    try {
      const o = JSON.parse(v);
      if (o && o.secret) out[k] = o.secret;
    } catch (e) {}
  }
  return out;
}
"""


def main() -> int:
    if not PROFILE_DIR.exists():
        print(f"[inspect] profile not found at {PROFILE_DIR}", file=sys.stderr)
        return 1

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(M365_URL, wait_until="domcontentloaded")

        print("\nBrowser opened. If NOT signed in / Copilot Chat not visible,")
        print("finish the sign-in flow AND send a 'hello' message so Sydney")
        print("mints its token. Then come back here and press Enter.\n")
        sys.stdin.readline()

        # 1) All localStorage entries at a glance
        rows = page.evaluate(_DUMP_ALL_JS) or []
        access_tokens = [r for r in rows if r.get("credentialType") == "AccessToken"]

        print(f"\n=== localStorage: {len(rows)} entries total, "
              f"{len(access_tokens)} AccessToken entries ===\n")

        for r in access_tokens:
            print("KEY   :", r["key"])
            print("  target   :", r.get("target"))
            print("  realm    :", r.get("realm"))
            print("  clientId :", r.get("clientId"))
            print("  expiresOn:", r.get("expiresOn"))
            print("  prefix   :", r.get("tokenPrefix"))
            print()

        # 2) Decode each JWT's aud/scp/appid so we can spot the Sydney one
        secrets = page.evaluate(_DUMP_SECRETS_JS) or {}
        print("=== Decoded JWT audiences ===\n")
        for k, secret in secrets.items():
            payload = _decode_jwt(secret)
            print(f"KEY: {k[:120]}{'…' if len(k) > 120 else ''}")
            print(f"  aud  : {payload.get('aud')}")
            print(f"  scp  : {payload.get('scp')}")
            print(f"  appid: {payload.get('appid')}")
            print(f"  tid  : {payload.get('tid')}")
            print()

        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
