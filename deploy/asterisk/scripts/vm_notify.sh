#!/bin/sh
# app_voicemail externnotify hook: <context> <mailbox> <new> <old> [<urgent>]
# Finds the newest message in the mailbox INBOX and POSTs it to DIAL's `voicemail` hook:
#     event, mailbox, caller, file_path, duration
# Event slug = context without the "dial-" prefix (mailboxes are <number>@dial-<slug>).
[ -f /etc/asterisk/dial.env ] && . /etc/asterisk/dial.env
: "${DIAL_API_URL:=http://dial:8000}"
: "${DIAL_PBX_HOOK_SECRET:=dial}"

CONTEXT="$1"
MAILBOX="$2"
NEW="${3:-0}"
OLD="${4:-0}"
case "$CONTEXT" in
    dial-*) EVENT="${CONTEXT#dial-}" ;;
    *) EVENT="$CONTEXT" ;;
esac

SPOOL="/var/spool/asterisk/voicemail/${CONTEXT}/${MAILBOX}/INBOX"
if [ ! -d "$SPOOL" ]; then
    exit 0
fi
# newest msgNNNN.txt (metadata written by app_voicemail)
META=$(ls -1t "$SPOOL"/msg*.txt 2>/dev/null | head -n1)
[ -n "$META" ] || exit 0
BASE="${META%.txt}"
AUDIO=""
for ext in wav WAV gsm wav49 ulaw; do
    [ -f "$BASE.$ext" ] && { AUDIO="$BASE.$ext"; break; }
done
[ -n "$AUDIO" ] || AUDIO="$BASE.wav"

# metadata is INI-like: callerid="Alice" <4242>, duration=12, origtime=...
CALLERID=$(sed -n 's/^callerid=//p' "$META" | head -n1)
CALLER=$(printf '%s' "$CALLERID" | sed -n 's/.*<\([^>]*\)>.*/\1/p')
[ -n "$CALLER" ] || CALLER=$(printf '%s' "$CALLERID" | tr -d '"')
DURATION=$(sed -n 's/^duration=//p' "$META" | head -n1)
: "${DURATION:=0}"

curl -fsS -m 5 -X POST \
    -H "X-DIAL-PBX-Secret: ${DIAL_PBX_HOOK_SECRET}" \
    --data-urlencode "event=${EVENT}" \
    --data-urlencode "mailbox=${MAILBOX}" \
    --data-urlencode "caller=${CALLER}" \
    --data-urlencode "file_path=${AUDIO}" \
    --data-urlencode "duration=${DURATION}" \
    --data-urlencode "new_messages=${NEW}" \
    --data-urlencode "old_messages=${OLD}" \
    "${DIAL_API_URL}/api/v1/pbx/hooks/voicemail/" >/dev/null 2>&1 || \
    logger -t dial-vm "failed to notify DIAL about ${MAILBOX}@${CONTEXT}"
exit 0
