#!/bin/bash
# One-shot setup for a fresh machine.
#
#   ./install.sh
#
# Creates the venv, installs Python deps, installs Playwright's Chromium,
# and symlinks bin/copilot into ~/.local/bin so `copilot` works globally.
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

echo
echo "[install] Done. Next steps:"
echo "  1. copilot login                    # first-time sign-in (browser)"
echo "  2. copilot                          # start chatting"
echo "  3. python app.py                    # (optional) OpenAI-compatible server"
echo
echo "[install] For Codex CLI integration, see MIGRATE.md."
