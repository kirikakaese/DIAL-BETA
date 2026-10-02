#!/bin/sh
# Emits the static [pet-<slug>] shell contexts for extensions.conf (#exec include).
#
# Each event needs a shell context so that pbx_realtime is consulted for it:
#     [pet-<slug>]
#     include => pet-internal
#     switch => Realtime/@
#
# 1) ask PET (GET /api/v1/pbx/dialplan/?shell=1 -> all non-archived events)
# 2) fall back to the PET_EVENTS environment variable (comma separated slugs)
# Runs at Asterisk start and on every `dialplan reload` (PET triggers one via AMI after sync_event).
[ -f /etc/asterisk/pet.env ] && . /etc/asterisk/pet.env
: "${PET_API_URL:=http://pet:8000}"
: "${PET_PBX_HOOK_SECRET:=pet}"
: "${PET_EVENTS:=}"

OUT=$(curl -fsS -m 4 -H "X-PET-PBX-Secret: ${PET_PBX_HOOK_SECRET}" \
      "${PET_API_URL}/api/v1/pbx/dialplan/?shell=1" 2>/dev/null || true)

if [ -n "$OUT" ] && printf '%s' "$OUT" | grep -q '^\[pet-'; then
    echo "; shell contexts fetched from ${PET_API_URL}"
    printf '%s\n' "$OUT"
    exit 0
fi

echo "; PET unreachable - shell contexts from PET_EVENTS='${PET_EVENTS}'"
OLDIFS=$IFS
IFS=','
for slug in $PET_EVENTS; do
    IFS=$OLDIFS
    slug=$(printf '%s' "$slug" | tr -d ' ')
    [ -n "$slug" ] || continue
    printf '[pet-%s]\ninclude => pet-internal\nswitch => Realtime/@\n\n' "$slug"
done
IFS=$OLDIFS
exit 0
