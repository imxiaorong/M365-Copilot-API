# M365-Copilot-API

[中文版](README.md)

Local OpenAI / Responses API-compatible server for **Microsoft 365 Copilot Chat**
(work/school account). Talks to the consumer web endpoint under
`m365.cloud.microsoft/chat` — no application registration, no admin consent,
no Graph API. You sign in once in a browser; the Sydney chat token is refreshed
silently after that.

> **Unofficial.** Automates a first-party web experience for personal use. Not
> affiliated with or endorsed by Microsoft. Use responsibly and inside your
> organisation's acceptable-use policy. See [DISCLAIMER](DISCLAIMER.md).

## Features

- **Drop-in OpenAI-compatible server** — works with Codex CLI, Claude Code,
  `openai` Python SDK, and any standard OpenAI-compatible client
- **Responses API** native — speaks the `wire_api = "responses"` protocol
  that Codex CLI v0.144+ requires
- **Streaming** — full SSE streaming support for both Chat Completions and
  Responses endpoints
- **Silent token refresh** — keeps the Sydney access token fresh without
  popping browser windows
- **cc-switch integration** — one-click provider setup via the install script

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
python app.py                    # http://127.0.0.1:8000
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

## Quick start

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

python -m m365copilot login      # visible browser; sign in with your M365 account
```

The persistent Playwright profile lives under `session/profile/`. As long as
its Entra refresh token is alive, subsequent runs read a fresh Sydney access
token silently — no re-login until the profile expires (typically weeks).

### One-shot install

```bash
./install.sh
```

This creates the venv, installs Python deps, downloads Playwright's Chromium,
and links the `copilot` CLI command to `~/.local/bin/copilot`. If
[cc-switch](https://github.com/your-org/cc-switch) is installed, it
automatically adds a managed M365 Copilot provider.

## CLI

```bash
copilot login              # interactive sign-in
copilot ask "Hello!"       # one-shot question, streams to stdout
copilot chat               # REPL that remembers the conversation
```

## Server endpoints

### OpenAI Chat Completions (`/v1/chat/completions`)

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | Supports `stream: true` and `conversation_id` pass-through |
| `GET`  | `/v1/models`           | Advertises tone-tagged model variants |
| `GET`  | `/healthz`             | Liveness probe |

### OpenAI Responses API (`/v1/responses`)

Codex CLI v0.144+ requires `wire_api = "responses"`. This server natively
speaks it — full SSE streaming, typed events, no adapter needed.

```bash
curl -X POST http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"input": "Hello!", "model": "m365-copilot"}'
```

### Model names → Copilot tone

| Model name | Copilot tone | Description |
| --- | --- | --- |
| `m365-copilot` | `Gpt_5_6_Reasoning` | Default — Thinker |
| `m365-copilot-thinker` | `Gpt_5_6_Reasoning` | Explicit Thinker |
| `m365-copilot-fast` | `Magic` | Non-thinker, faster |
| `m365-copilot-creative` | `Creative` | Legacy Bing tone |
| `m365-copilot-precise` | `Precise` | Legacy Bing tone |
| `m365-copilot-balanced` | `Balanced` | Legacy Bing tone |

### Env vars

| Variable | Default | Description |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Listen address |
| `PORT` | `8000` | Listen port |
| `RATE_LIMIT_RPM` | `20` | Requests per minute (0 = disabled) |
| `RATE_LIMIT_BURST` | `5` | Burst capacity |
| `MODEL_NAME` | `m365-copilot` | Advertised model name |

## Codex CLI integration

### Via cc-switch (recommended)

Run `./install.sh` and it will auto-configure cc-switch. Or manually add a
provider in the cc-switch UI with:

- `base_url`: `http://localhost:8000/v1`
- `api_key`: `unused`
- `wire_api`: `responses`

### Manual config

Append to `~/.codex/config.toml`:

```toml
[profiles.m365]
model = "m365-copilot"
model_provider = "m365"

[model_providers.m365]
name = "M365 Copilot"
base_url = "http://localhost:8000/v1"
wire_api = "responses"
env_key = "M365_KEY"
```

Then in `~/.bash_profile` or `~/.zshrc`:

```bash
export M365_KEY=unused
alias codex-m365='codex --profile m365'
```

## Project layout

```
m365copilot/       # library
├── auth.py           token cache + expiry checks
├── browser.py        Playwright sign-in + Sydney-token capture
├── protocol.py       SignalR framing + invocation payload builders
├── driver.py         pure WebSocket Chathub driver
├── silent_refresh.py Entra refresh-token exchange (no browser)
└── client.py         high-level API (chat/stream, conversation continuation)

server/            # OpenAI-compatible FastAPI bridge
├── api.py            /v1/chat/completions, /v1/responses, /v1/models, /healthz
├── prompt.py         OpenAI messages → single Copilot prompt
├── openai_format.py  Chat Completion shaping
├── responses_format.py  Responses API shaping
├── schemas.py        pydantic request models
├── ratelimit.py      token-bucket limiter
├── lock.py           shared upstream lock
├── keepalive.py      background token refresher
└── config.py         env-driven server configuration

tools/
├── capture_m365.py   protocol capture helper (Playwright)
├── diag_headless.py  diagnose headless token capture issues
└── inspect_tokens.py inspect MSAL token cache
```

## Protocol notes

Reverse-engineered from a captured web session — see `captures/chat_frames.md`
after running `tools/capture_m365.py`. Key facts:

- Chat lives at `wss://substrate.office.com/m365Copilot/Chathub/{oid}@{tid}`
- Transport is SignalR JSON hub protocol, records separated by `\x1e`
- Turn shape: handshake → ping → `type:4` invocation → many `type:1` updates
  (each carries a growing full-replace `messages[0].text`) → `type:2`
  completion (has `conversationId`, final `result.message`) → `type:3` ack
- Auth is a Bearer JWT audienced to `https://substrate.office.com/sydney`.
  Read from MSAL localStorage under the
  `https://substrate.office.com/sydney/.default` cache key
- Token lives ~60–75 min; we refresh at 50 min and honour the JWT `exp` claim

If Microsoft changes the wire format, re-capture with
`python tools/capture_m365.py` and diff `chat_frames.md`.

## Limitations

- One Sydney account can't cleanly serve parallel conversations — the server
  serialises upstream calls behind an asyncio lock. Throughput is sequential.
- Copilot's per-conversation cap is 600 user messages (from the throttling
  frame). Per-day quotas vary by license — see `throttling.metering` on any
  completion frame for what's left.
- Conditional-access-locked tenants may reject the Playwright login (unmanaged
  device, wrong browser). If sign-in fails with a policy prompt, this project
  can't get past it.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). This is a personal tool, not a
commercial product — keep PRs focused and aligned with the project scope.

## License

MIT. See [LICENSE](LICENSE).