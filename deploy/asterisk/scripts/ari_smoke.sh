#!/bin/sh
# Quick smoke test from inside the container (or any host with curl):
#   ./ari_smoke.sh [ari-base-url] [user] [password]
URL="${1:-${ARI_URL:-http://127.0.0.1:8088/ari}}"
USER="${2:-${ARI_USER:-pet}}"
PASS="${3:-${ARI_PASSWORD:-pet}}"
set -e
echo "== asterisk/info"
curl -fsS -u "$USER:$PASS" "$URL/asterisk/info" | jq '.system.version, .status'
echo "== endpoints (realtime ps_endpoints visible to PJSIP)"
curl -fsS -u "$USER:$PASS" "$URL/endpoints/PJSIP" | jq -r '.[] | "\(.resource)\t\(.state)\t\(.channel_ids|length) calls"'
echo "== channels"
curl -fsS -u "$USER:$PASS" "$URL/channels" | jq -r '.[] | "\(.id)\t\(.name)\t\(.state)\t\(.caller.number) -> \(.dialplan.exten)"'
