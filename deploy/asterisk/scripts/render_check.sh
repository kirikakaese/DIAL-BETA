#!/bin/sh
# Render the config templates exactly like entrypoint.sh does, into a scratch directory, and check
# that only the PET placeholders were substituted (Asterisk's own ${EXTEN}-style variables survive).
# Usage: scripts/render_check.sh [outdir]      (needs `envsubst` from gettext)
set -eu
HERE=$(cd "$(dirname "$0")/.." && pwd)
OUT="${1:-${TMPDIR:-/tmp}/pet-asterisk-render}"
mkdir -p "$OUT"

: "${PET_API_URL:=http://pet:8000}"; : "${PET_PBX_HOOK_SECRET:=pet}"; : "${PET_EMERGENCY_FALLBACK:=}"
: "${PET_CDR_HOOK:=no}"; : "${DB_HOST:=db}"; : "${DB_PORT:=5432}"; : "${DB_NAME:=pet}"; : "${DB_USER:=pet}"
: "${DB_PASSWORD:=pet}"; : "${ARI_USER:=pet}"; : "${ARI_PASSWORD:=pet}"; : "${AMI_USER:=pet}"
: "${AMI_PASSWORD:=pet}"; : "${SIP_DOMAIN:=pet.local}"; : "${RTP_START:=10000}"; : "${RTP_END:=10200}"
export PET_API_URL PET_PBX_HOOK_SECRET PET_EMERGENCY_FALLBACK PET_CDR_HOOK DB_HOST DB_PORT DB_NAME DB_USER \
       DB_PASSWORD ARI_USER ARI_PASSWORD AMI_USER AMI_PASSWORD SIP_DOMAIN RTP_START RTP_END

VARS='${PET_API_URL} ${PET_PBX_HOOK_SECRET} ${PET_EMERGENCY_FALLBACK} ${PET_CDR_HOOK} ${DB_HOST} ${DB_PORT} ${DB_NAME} ${DB_USER} ${DB_PASSWORD} ${ARI_USER} ${ARI_PASSWORD} ${AMI_USER} ${AMI_PASSWORD} ${SIP_DOMAIN} ${RTP_START} ${RTP_END}'
for f in "$HERE"/conf/*.conf "$HERE"/conf/*.ini; do
    envsubst "$VARS" < "$f" > "$OUT/$(basename "$f")"
done

rc=0
# 1) no PET placeholder left behind
if grep -nE '\$\{(PET_API_URL|PET_PBX_HOOK_SECRET|DB_[A-Z]+|ARI_[A-Z]+|AMI_[A-Z]+|SIP_DOMAIN|RTP_[A-Z]+)\}' "$OUT"/*; then
    echo "FAIL: unsubstituted placeholders"; rc=1
fi
# 2) Asterisk variables untouched
a=$(grep -c '\${EXTEN' "$HERE/conf/extensions.conf"); b=$(grep -c '\${EXTEN' "$OUT/extensions.conf")
[ "$a" = "$b" ] || { echo "FAIL: \${EXTEN} count changed ($a -> $b)"; rc=1; }
# 3) the important values landed where Asterisk expects them
grep -q "^PET_API=$PET_API_URL" "$OUT/extensions.conf" || { echo "FAIL: PET_API global"; rc=1; }
grep -q "^\[$ARI_USER\]" "$OUT/ari.conf" || { echo "FAIL: ari user"; rc=1; }
grep -q "^\[$AMI_USER\]" "$OUT/manager.conf" || { echo "FAIL: ami user"; rc=1; }
grep -q "^Servername = $DB_HOST" "$OUT/odbc.ini" || { echo "FAIL: odbc host"; rc=1; }
grep -q "^rtpstart=$RTP_START" "$OUT/rtp.conf" || { echo "FAIL: rtp"; rc=1; }
[ $rc -eq 0 ] && echo "OK: rendered $(ls "$OUT" | wc -l | tr -d ' ') files into $OUT"
exit $rc
