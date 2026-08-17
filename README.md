# M365-Copilot-API

Local OpenAI-compatible API for Microsoft 365 Copilot Chat (work/school
account). Talks to the consumer web endpoint under
`m365.cloud.microsoft/chat` — no application registration, no admin consent,
no Graph API. You sign in once in a browser; the Sydney chat token is read
out of MSAL localStorage and reused headlessly after that.

> Unofficial. Automates a first-party web experience for personal use. Not
> affiliated with or endorsed by Microsoft — use responsibly and inside
> your organisation's acceptable-use policy.

## Two ways to use it

**As a Python library**:

```python
from m365copilot import M365CopilotClient

client = M365CopilotClient()
reply = client.chat("Say hello.")
print(reply.text, reply.conversation_id)

reply2 = client.chat("And in French?", reply.conversation_id)   # continue
for chunk in client.stream("Tell me a joke"):
    print(chunk, end="", flush=True)
```

**As a local OpenAI-compatible server** (drop-in for the `openai` SDK):

```bash
python app.py                    # http://127.0.0.1:8000/v1
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")
resp = client.chat.completions.create(
    model="m365-copilot",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

python -m m365copilot login      # visible browser; sign in with your M365 account
```

The persistent Playwright profile lives under `session/profile/`. As long as
its Entra refresh token is alive, subsequent runs read a fresh Sydney access
token headlessly — no re-login until the profile expires (typically weeks).

## CLI

```bash
python -m m365copilot login              # interactive sign-in
python -m m365copilot ask "Hello!"       # one-shot question, streams to stdout
python -m m365copilot chat               # REPL that remembers the conversation
```

## Endpoints (server)

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | Supports `stream: true` and a custom `conversation_id` pass-through |
| `GET`  | `/v1/models`           | Advertises a single `m365-copilot` model |
| `GET`  | `/healthz`             | Liveness probe |

Env vars: `HOST` (default `127.0.0.1`), `PORT` (`8000`), `RATE_LIMIT_RPM` (`20`),
`RATE_LIMIT_BURST` (`5`), `MODEL_NAME` (`m365-copilot`).

## Protocol notes

Reverse-engineered from a captured web session (see
`captures/chat_frames.md` after running `tools/capture_m365.py`). Key facts:

- Chat lives at `wss://substrate.office.com/m365Copilot/Chathub/{oid}@{tid}`
- Transport is SignalR JSON hub protocol, records separated by `\x1e`
- Turn shape: handshake → ping → `type:4` invocation → many `type:1` updates
  (each carries a growing full-replace `messages[0].text`) → `type:2`
  completion (has `conversationId`, final `result.message`) → `type:3` ack
- Auth is a Bearer JWT audienced to `https://substrate.office.com/sydney`.
  Read from MSAL localStorage under the
  `https://substrate.office.com/sydney/.default` cache key
- Token lives ~60-75 min; we refresh at 50 min and honour the JWT `exp` claim

If Microsoft changes the wire format, re-capture with
`python tools/capture_m365.py` and diff `chat_frames.md`.

## Project layout

```
m365copilot/       # library
├── auth.py           token cache + expiry checks
├── browser.py        Playwright sign-in + Sydney-token capture
├── protocol.py       SignalR framing + invocation payload builders
├── driver.py         pure WebSocket Chathub driver
└── client.py         high-level API (chat/stream, conversation continuation)

server/            # OpenAI-compatible FastAPI bridge
├── api.py            /v1/chat/completions, /v1/models, /healthz
├── prompt.py         OpenAI messages → single Copilot prompt
├── openai_format.py  non-streaming ChatCompletion shaping
├── schemas.py        pydantic request models
└── ratelimit.py      token-bucket limiter

tools/
└── capture_m365.py   protocol capture helper (Playwright)
```

## Notes & limits

- One Sydney account can't cleanly serve parallel conversations — the server
  serialises upstream calls behind an asyncio lock. Throughput is sequential.
- Copilot's per-conversation cap is 600 user messages (from the throttling
  frame). Per-day quotas vary by license — see `throttling.metering` on any
  completion frame for what's left.
- Conditional-access-locked tenants may reject the Playwright login (unmanaged
  device, wrong browser). If sign-in fails with a policy prompt, this project
  can't get past it — nothing to do on our side.

## License

MIT.
