#!/bin/bash
# One-shot setup for a fresh machine.
#
#   ./install.sh
#
# Creates the venv, installs Python deps, installs Playwright's Chromium,
# symlinks bin/copilot into ~/.local/bin so `copilot` works globally,
# and optionally registers a M365 Copilot provider in cc-switch.
# Idempotent — safe to re-run.

set -e

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJ"

echo "[install] Project root: $PROJ"

# 1. venv
if [ ! -d venv ]; then
  echo "[install] Creating venv..."
  python3 -m venv venv
fi

# 2. Python deps
echo "[install] Installing Python dependencies..."
./venv/bin/pip install --upgrade pip >/dev/null
./venv/bin/pip install -r requirements.txt

# 3. Playwright Chromium (one-time download, ~170 MB)
if [ ! -d "$HOME/Library/Caches/ms-playwright" ] || \
   ! ls "$HOME/Library/Caches/ms-playwright"/chromium* >/dev/null 2>&1; then
  echo "[install] Installing Playwright Chromium (~170 MB)..."
  ./venv/bin/python -m playwright install chromium
fi

# 4. Global `copilot` command
mkdir -p "$HOME/.local/bin"
chmod +x bin/copilot
ln -sf "$PROJ/bin/copilot" "$HOME/.local/bin/copilot"
echo "[install] Symlinked $HOME/.local/bin/copilot -> $PROJ/bin/copilot"

if ! echo ":$PATH:" | grep -q ":$HOME/.local/bin:"; then
  echo
  echo "[install] WARNING: $HOME/.local/bin is not on your PATH."
  echo "  Add this line to your ~/.bash_profile or ~/.zshrc:"
  echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# 5. cc-switch integration (optional) — register a M365 Copilot provider
#    for Codex CLI so it appears as a selectable provider in the cc-switch UI.
CC_SWITCH_DB="$HOME/.cc-switch/cc-switch.db"
if [ -f "$CC_SWITCH_DB" ]; then
  echo "[install] cc-switch detected — registering M365 Copilot provider..."
  TMP_SCRIPT=$(mktemp)
  cat > "$TMP_SCRIPT" << 'PYEOF'
import json, os, sqlite3, uuid, time

db_path = os.environ['CC_SWITCH_DB']
conn = sqlite3.connect(db_path)
cur = conn.cursor()

existing = cur.execute(
    "SELECT id FROM providers WHERE app_type = 'codex' AND name = 'M365 Copilot'"
).fetchone()
if existing:
    print("  Provider already registered (id=%s), skipping." % existing[0])
else:
    pid = str(uuid.uuid4())
    config = {
        "auth": {"OPENAI_API_KEY": "unused"},
        "config": (
            'model_provider = "custom"\n'
            'model = "m365-copilot"\n'
            'disable_response_storage = true\n'
            '\n'
            '[model_providers.custom]\n'
            'name = "M365 Copilot"\n'
            'wire_api = "responses"\n'
            'requires_openai_auth = true\n'
            'base_url = "http://localhost:8000/v1"\n'
        ),
        "modelCatalog": {
            "models": [
                {"model": "m365-copilot", "displayName": "m365-copilot", "contextWindow": 32000}
            ]
        },
    }
    meta = json.dumps({
        "commonConfigEnabled": True,
        "endpointAutoSelect": True,
        "apiFormat": "openai_responses",
    })
    now = int(time.time())
    cur.execute(
        """INSERT INTO providers (id, app_type, name, settings_config, website_url,
           category, created_at, sort_index, notes, icon, icon_color, meta,
           is_current, in_failover_queue, cost_multiplier, provider_type)
           VALUES (?, 'codex', 'M365 Copilot', ?, NULL, 'custom',
           ?, 99, 'Local M365 Copilot Chat API', 'robot', '#0078D4',
           ?, 0, 0, '1.0', 'custom')""",
        (pid, json.dumps(config, ensure_ascii=False), now, meta),
    )
    conn.commit()
    print("  Registered provider id=%s" % pid)
    print("  Open cc-switch -> Codex -> select 'M365 Copilot' to use it.")

conn.close()
PYEOF
  CC_SWITCH_DB="$CC_SWITCH_DB" "$PROJ/venv/bin/python" "$TMP_SCRIPT" 2>&1
  rm -f "$TMP_SCRIPT"
else
  echo "[install] cc-switch not detected (no $CC_SWITCH_DB)."
  echo "  To register a provider manually, open cc-switch -> Codex -> Add provider"
  echo "  with base_url=http://localhost:8000/v1, api_key=unused, wire_api=responses."
fi

echo
echo "[install] Done. Next steps:"
echo "  1. Start the server:  python app.py"
echo "  2. Sign in:           copilot login"
echo "  3. Try it:            copilot ask \"Hello!\""
echo
echo "[install] cc-switch users: open cc-switch → Codex → select 'M365 Copilot'"
echo "  as the active provider. Make sure the server is running (step 1) first."
echo
echo "[install] Manual Codex CLI config (no cc-switch): see MIGRATE.md."
