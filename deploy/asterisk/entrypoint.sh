#!/bin/sh
# DIAL Asterisk entrypoint: render config templates from environment, generate a
# self-signed TLS certificate when none is mounted, wait for PostgreSQL, start Asterisk.
#
# Environment (all optional, defaults in brackets):
#   DIAL_API_URL            [http://dial:8000]      base URL of DIAL (hooks + route API)
#   DIAL_PBX_HOOK_SECRET    [dial]                  must equal DIAL's DIAL_PBX_HOOK_SECRET / ASTERISK_ARI_PASSWORD
#   DIAL_EVENTS             []                     comma separated event slugs (fallback shell contexts if DIAL is down)
#   DIAL_EMERGENCY_FALLBACK []                     number dialled when DIAL has no emergency route
#   DIAL_CDR_HOOK           [no]                   yes -> POST every CDR to the cdr hook as well
#   DIAL_RECORD_DIR         []                     override where announcements recorded by phone are written
#                                                 (default: the path DIAL returns = its DIAL_RECORDING_DIR,
#                                                 /var/spool/asterisk/dial-recordings; share that volume with DIAL)
#   DB_HOST                [db]     DB_PORT [5432]   DB_NAME [dial]      (DIAL's PostgreSQL - shared!)
#   DB_USER                [dial]    DB_PASSWORD [dial]                   (DATABASE_* accepted as aliases)
#   ARI_USER               [dial]    ARI_PASSWORD  [dial]   must match DIAL's ASTERISK_ARI_USER/PASSWORD
#   AMI_USER               [dial]    AMI_PASSWORD  [dial]   must match DIAL's ASTERISK_AMI_USER/PASSWORD
#   SIP_DOMAIN             [dial.local]            default_realm for digest auth
#   EXTERNAL_IP            []                     public/host IP for NAT'd SIP/RTP (docker bridge networks;
#                                                 SIP_EXTERNAL_IP accepted as alias)
#   RTP_START / RTP_END    [10000 / 10200]
#   TLS_CERT_CN            [$SIP_DOMAIN]          CN of the self-signed certificate
set -eu

: "${DIAL_API_URL:=http://dial:8000}"
: "${DIAL_PBX_HOOK_SECRET:=dial}"
: "${DIAL_EVENTS:=}"
: "${DIAL_EMERGENCY_FALLBACK:=}"
: "${DIAL_CDR_HOOK:=no}"
: "${DIAL_RECORD_DIR:=}"
: "${DB_HOST:=${DATABASE_HOST:-db}}"
: "${DB_PORT:=${DATABASE_PORT:-5432}}"
: "${DB_NAME:=${DATABASE_NAME:-dial}}"
: "${DB_USER:=${DATABASE_USER:-dial}}"
: "${DB_PASSWORD:=${DATABASE_PASSWORD:-dial}}"
: "${ARI_USER:=dial}"
: "${ARI_PASSWORD:=dial}"
: "${AMI_USER:=dial}"
: "${AMI_PASSWORD:=dial}"
: "${SIP_DOMAIN:=dial.local}"
: "${EXTERNAL_IP:=${SIP_EXTERNAL_IP:-}}"
: "${RTP_START:=10000}"
: "${RTP_END:=10200}"
: "${TLS_CERT_CN:=$SIP_DOMAIN}"
export DIAL_API_URL DIAL_PBX_HOOK_SECRET DIAL_EVENTS DIAL_EMERGENCY_FALLBACK DIAL_CDR_HOOK DIAL_RECORD_DIR \
       DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD \
       ARI_USER ARI_PASSWORD AMI_USER AMI_PASSWORD SIP_DOMAIN EXTERNAL_IP RTP_START RTP_END

TEMPLATES=/opt/dial/conf
TARGET=/etc/asterisk
# Only these placeholders are substituted; Asterisk's own ${EXTEN}-style variables are left alone.
VARS='${DIAL_API_URL} ${DIAL_PBX_HOOK_SECRET} ${DIAL_EMERGENCY_FALLBACK} ${DIAL_CDR_HOOK} ${DB_HOST} ${DB_PORT} ${DB_NAME} ${DB_USER} ${DB_PASSWORD} ${ARI_USER} ${ARI_PASSWORD} ${AMI_USER} ${AMI_PASSWORD} ${SIP_DOMAIN} ${RTP_START} ${RTP_END}'

echo "[dial] rendering configuration into $TARGET"
mkdir -p "$TARGET/keys"
for f in "$TEMPLATES"/*.conf "$TEMPLATES"/*.ini; do
    [ -f "$f" ] || continue
    envsubst "$VARS" < "$f" > "$TARGET/$(basename "$f")"
done

# ODBC: driver manager config (path of psqlodbcw.so differs per architecture)
DRIVER=$(find /usr/lib -name 'psqlodbcw.so' 2>/dev/null | head -n1 || true)
if [ -n "$DRIVER" ]; then
    sed -i "s|__PSQLODBC_DRIVER__|$DRIVER|" "$TARGET/odbcinst.ini"
    cp "$TARGET/odbcinst.ini" /etc/odbcinst.ini
    cp "$TARGET/odbc.ini" /etc/odbc.ini
else
    echo "[dial] WARNING: PostgreSQL ODBC driver not found; realtime will not work" >&2
fi

# NAT: publish the host IP in SIP/SDP when running on a bridge network
if [ -n "$EXTERNAL_IP" ]; then
    sed -i "s|^;external_media_address=.*|external_media_address=$EXTERNAL_IP|; s|^;external_signaling_address=.*|external_signaling_address=$EXTERNAL_IP|" "$TARGET/pjsip.conf"
fi

# Environment file consumed by #exec scripts and the voicemail notify hook
cat > "$TARGET/dial.env" <<EOF
DIAL_API_URL='$DIAL_API_URL'
DIAL_PBX_HOOK_SECRET='$DIAL_PBX_HOOK_SECRET'
DIAL_EVENTS='$DIAL_EVENTS'
EOF
chmod 640 "$TARGET/dial.env"

# TLS: self-signed certificate for transport-tls / WSS unless one is mounted
KEYDIR="$TARGET/keys"
if [ ! -s "$KEYDIR/asterisk.pem" ]; then
    echo "[dial] generating self-signed TLS certificate for $TLS_CERT_CN"
    openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=$TLS_CERT_CN/O=DIAL" \
        -keyout "$KEYDIR/asterisk.key" -out "$KEYDIR/asterisk.crt" >/dev/null 2>&1
    cat "$KEYDIR/asterisk.key" "$KEYDIR/asterisk.crt" > "$KEYDIR/asterisk.pem"
fi
chmod 600 "$KEYDIR"/asterisk.* || true
# Announcements recorded by phone (Record() in [dial-services] record-announcement); DIAL imports from here
mkdir -p "${DIAL_RECORD_DIR:-/var/spool/asterisk/dial-recordings}"
chown -R asterisk:asterisk "$TARGET" /var/lib/asterisk /var/spool/asterisk /var/log/asterisk /var/run/asterisk 2>/dev/null || true

# Wait for PostgreSQL (realtime modules preload and want a working connection)
if command -v isql >/dev/null 2>&1; then
    i=0
    until echo "select 1" | isql -b asterisk "$DB_USER" "$DB_PASSWORD" >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 30 ]; then
            echo "[dial] WARNING: database $DB_HOST:$DB_PORT/$DB_NAME not reachable after 60s - starting anyway" >&2
            break
        fi
        echo "[dial] waiting for database ($i/30)..."
        sleep 2
    done
fi

echo "[dial] starting: $*"
exec "$@"
