#!/usr/bin/env bash
# Set up claude-garmin-coach on a headless Ubuntu server.
# Run from the claude-garmin-coach folder as the user that will run the bot (not root).
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
GARMIN_MCP_SRC="${GARMIN_MCP_SRC:-git+https://github.com/Taxuspt/garmin_mcp}"

echo "==> Python venv with garmin-mcp ($GARMIN_MCP_SRC)"
if [[ "$GARMIN_MCP_SRC" == git+* ]] && ! command -v git >/dev/null; then
    echo "git is needed to install from GitHub: sudo apt install git" >&2
    echo "(or copy the garmin_mcp repo here and run: GARMIN_MCP_SRC=/path/to/garmin_mcp ./install.sh)" >&2
    exit 1
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade "$GARMIN_MCP_SRC"

echo "==> Claude Code"
if command -v claude >/dev/null || [ -x "$HOME/.local/bin/claude" ]; then
    echo "already installed"
else
    curl -fsSL https://claude.ai/install.sh | bash
fi

echo "==> coach notes"
mkdir -p coach/notes
for f in coach/notes-template/*.md; do
    [ -f "coach/notes/$(basename "$f")" ] || cp "$f" coach/notes/
done

echo "==> config.env"
[ -f config.env ] || cp config.env.example config.env
chmod 600 config.env

echo "==> writable paths for the sandboxed service"
# The unit makes everything read-only except these; they must exist before it starts.
mkdir -p "$HOME/.claude" "$HOME/.garminconnect"
[ -f "$HOME/.claude.json" ] || echo '{}' > "$HOME/.claude.json"

echo "==> systemd unit (claude-garmin-coach.service)"
sed -e "s|@USER@|$(id -un)|g" -e "s|@DIR@|$DIR|g" -e "s|@HOME@|$HOME|g" \
    claude-garmin-coach.service.in > claude-garmin-coach.service

cat <<EOF

Done. Next steps (details in README.md):
  1. Garmin tokens in ~/.garminconnect: copy them from another machine, or run
       .venv/bin/garmin-mcp-auth
  2. Claude login:   ~/.local/bin/claude setup-token   -> put the token in config.env
  3. Telegram:       put TELEGRAM_BOT_TOKEN in config.env
  4. Test:           .venv/bin/python bot.py --ask "яке в мене наступне тренування?"
  5. Personalize:    coach/notes/*.md and, optionally, coach/CLAUDE.local.md
  6. Start service:  sudo cp claude-garmin-coach.service /etc/systemd/system/
                     sudo systemctl daemon-reload
                     sudo systemctl enable --now claude-garmin-coach
                     journalctl -u claude-garmin-coach -f
EOF
