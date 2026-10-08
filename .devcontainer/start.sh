#!/usr/bin/env bash
# Runs every time the codespace starts: DIAL on port 8000 in the background (log: /tmp/dial.log).
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python manage.py migrate --noinput >/tmp/dial-migrate.log 2>&1 || true
setsid nohup .venv/bin/python manage.py runserver 0.0.0.0:8000 >/tmp/dial.log 2>&1 < /dev/null &
echo "DIAL starts on port 8000 (log: /tmp/dial.log). Login: admin@dial.local / admin"
