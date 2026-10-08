#!/usr/bin/env bash
# Runs once when the codespace is created: dependencies, a .env for the codespace, database and demo data.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    url="http://localhost:8000"
    if [ -n "${CODESPACE_NAME:-}" ]; then
        url="https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
    fi
    cat > .env <<ENV
# Written by .devcontainer/setup.sh for this codespace (development only, never use these values in production).
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(50))")
DEBUG=1
ALLOWED_HOSTS=*
CSRF_TRUSTED_ORIGINS=https://*.app.github.dev,http://localhost:8000
DIAL_PUBLIC_URL=${url}
DIAL_TRUST_PROXY_HEADERS=1
MEDIA_ROOT=media
ENV
fi

make dev
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py seed_demo
