"""Diagnose why headless keep-alive can't capture a Sydney token.

Runs a headless Playwright against session/profile, logs every 1s whether:
  * MSAL says signed-in
  * current page URL
  * we've captured a token off passive traffic
  * MSAL cache has a Sydney token entry

Then triggers a warm-up (send "hi" into composer) and keeps polling. Useful
to see WHERE the pipeline stalls: sign-in never detected? warm-up composer
not found? WS never opens?

Run with server stopped (Playwright profile is exclusive):
    ./venv/bin/python tools/diag_headless.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from m365copilot.browser import BrowserAuth
from m365copilot.auth import DEFAULT_PROFILE_DIR


def main() -> int:
    bot = BrowserAuth(profile_dir=DEFAULT_PROFILE_DIR, headless=True)
    try:
        bot._start()
        print("[t=0] page URL after goto:", bot._page.url)

        for i in range(1, 31):
            time.sleep(1)
            signed_in = bot._signed_in()
            url = bot._page.url
            tok = bot._captured_token
            msal = bot._read_msal_sydney()
            print(f"[t={i}s] signed_in={signed_in} "
                  f"url={url[:80]} "
                  f"captured={'yes' if tok else 'no'} "
                  f"msal={'yes' if msal else 'no'}")
            if tok or msal:
                print("=" * 50)
                print("GOT TOKEN — stopping early")
                return 0

        print()
        print("[warm-up] navigating to /chat, sending 'hi'")
        bot._navigate_to_chat()
        sent = bot._send_warmup()
        print("[warm-up] send returned:", sent)

        for i in range(1, 21):
            time.sleep(1)
            tok = bot._captured_token
            msal = bot._read_msal_sydney()
            print(f"[after-warmup t={i}s] "
                  f"captured={'yes' if tok else 'no'} "
                  f"msal={'yes' if msal else 'no'}")
            if tok or msal:
                print("=" * 50)
                print("GOT TOKEN AFTER WARM-UP")
                return 0

        print()
        print("FAILED — no token captured within diagnostic window")
        return 1
    finally:
        bot.close()


if __name__ == "__main__":
    sys.exit(main())
