"""``python -m m365copilot`` CLI.

    python -m m365copilot login              # interactive sign-in
    python -m m365copilot ask "Hello"        # one-shot question, streams to stdout
    python -m m365copilot chat               # interactive REPL (persists conversation)
"""

from __future__ import annotations

import sys

from .auth import DEFAULT_AUTH_FILE, DEFAULT_PROFILE_DIR
from .client import M365CopilotClient


def _cmd_login() -> int:
    from .browser import BrowserAuth

    auth = BrowserAuth(profile_dir=DEFAULT_PROFILE_DIR, headless=False).login(
        path=DEFAULT_AUTH_FILE
    )
    return 0 if auth.get("access_token") else 1


def _cmd_ask(prompt: str) -> int:
    client = M365CopilotClient()
    for chunk in client.stream(prompt):
        if isinstance(chunk, str):
            sys.stdout.write(chunk)
            sys.stdout.flush()
    sys.stdout.write("\n")
    return 0


def _cmd_chat() -> int:
    client = M365CopilotClient()
    conv_id = None
    print("[m365copilot] Type your message. Blank line or Ctrl-D to exit.")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            return 0
        stream = client.stream(line, conversation_id=conv_id)
        for chunk in stream:
            if isinstance(chunk, str):
                sys.stdout.write(chunk)
                sys.stdout.flush()
        sys.stdout.write("\n")
        conv_id = stream.conversation_id


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]

    if cmd == "login":
        return _cmd_login()
    if cmd == "ask":
        if not args:
            print("usage: python -m m365copilot ask \"your question\"", file=sys.stderr)
            return 2
        return _cmd_ask(" ".join(args))
    if cmd == "chat":
        return _cmd_chat()

    print(f"unknown command: {cmd}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
