#!/bin/sh
# Emits the static [dial-<slug>] shell contexts for extensions.conf (#exec include).
#
# Each event needs a shell context so that pbx_realtime is consulted for it:
#     [dial-<slug>]
#     include => dial-internal
#     switch => Realtime/@
#
# 1) ask DIAL (GET /api/v1/pbx/dialplan/?shell=1 -> all non-archived events)
# 2) fall back to the DIAL_EVENTS environment variable (comma separated slugs)
# Runs at Asterisk start and on every `dialplan reload` (DIAL triggers one via AMI after sync_event).
[ -f /etc/asterisk/dial.env ] && . /etc/asterisk/dial.env
: "${DIAL_API_URL:=http://dial:8000}"
: "${DIAL_PBX_HOOK_SECRET:=dial}"
: "${DIAL_EVENTS:=}"

OUT=$(curl -fsS -m 4 -H "X-DIAL-PBX-Secret: ${DIAL_PBX_HOOK_SECRET}" \
      "${DIAL_API_URL}/api/v1/pbx/dialplan/?shell=1" 2>/dev/null || true)

if [ -n "$OUT" ] && printf '%s' "$OUT" | grep -q '^\[dial-'; then
    echo "; shell contexts fetched from ${DIAL_API_URL}"
    printf '%s\n' "$OUT"
    exit 0
fi

echo "; DIAL unreachable - shell contexts from DIAL_EVENTS='${DIAL_EVENTS}'"
OLDIFS=$IFS
IFS=','
for slug in $DIAL_EVENTS; do
    IFS=$OLDIFS
    slug=$(printf '%s' "$slug" | tr -d ' ')
    [ -n "$slug" ] || continue
    printf '[dial-%s]\ninclude => dial-internal\nswitch => Realtime/@\n\n' "$slug"
done
IFS=$OLDIFS
exit 0
