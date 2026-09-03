#!/usr/bin/env bash
# Start FitAcademie Squash Webapp
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
export PYTHONPATH="$SCRIPT_DIR:$PYTHONPATH"

# Load .env
set -a
source "$SCRIPT_DIR/.env"
set +a

# Override DATABASE_URL with absolute path
export DATABASE_URL="sqlite+aiosqlite:///${SCRIPT_DIR}/data/app.db"

# Ensure data directory exists
mkdir -p "$SCRIPT_DIR/data"

echo "🏸 FitAcademie Squash Webapp"
echo "   Listening on http://$HOST:$PORT"
echo "   DB: $DATABASE_URL"
echo ""

exec ~/.local/share/pipx/venvs/playwright/bin/python "$SCRIPT_DIR/main.py"