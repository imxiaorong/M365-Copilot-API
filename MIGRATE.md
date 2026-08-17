# Migrating to a new machine

## 1. Unpack and install

```bash
tar -xzvf M365-Copilot-API.tar.gz
cd M365-Copilot-API
./install.sh
```

`install.sh` handles:

- `venv/` creation + `pip install -r requirements.txt`
- Playwright Chromium download (~170 MB, one-time)
- Symlinking `bin/copilot` into `~/.local/bin/copilot` so `copilot` works
  globally
- cc-switch provider registration (if cc-switch is installed)

## 2. First-time sign-in

```bash
copilot login
```

A Chromium window opens. Sign in with your M365 account, then send one
message (e.g. `hi`) in the chat — the Sydney access token is captured off
the WebSocket URL and saved to `session/token.json`. No terminal Enter
needed; capture is automatic.

## 3. Everything use

```bash
copilot                    # interactive REPL
copilot ask "..."          # one-shot question
copilot login              # re-run sign-in when the token/refresh expires
```

## 4. OpenAI-compatible server (optional)

```bash
python app.py              # http://127.0.0.1:8000
```

## 5. Codex CLI integration

### Via cc-switch (recommended)

If cc-switch is installed, the `install.sh` script automatically registers a
"M365 Copilot" provider. Just open cc-switch → Codex → select "M365 Copilot"
as the active provider.

### Or manual config

For Codex CLI without cc-switch, append to `~/.codex/config.toml` (do NOT
touch the top-level `model` or `model_provider` — those belong to Codex
Desktop):

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

Reload the shell, start the server, and use it:

```bash
python app.py &                      # background the server
codex-m365 "write a fibonacci fn"
```

## What was NOT bundled

- `venv/` — recreated by `install.sh`
- `session/` — your M365 login state; re-run `copilot login` on the new machine
- `captures/` — protocol captures from the old machine, not useful elsewhere
- `.git/`, `__pycache__/`, `.DS_Store` — noise

Copilot session lives entirely on-disk in `session/` (git-ignored). Do not
copy `session/` from the old machine — the token is bound to that browser
profile's cookies and won't decrypt on a different Chromium install.