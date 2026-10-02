#!/bin/sh
# PET Asterisk entrypoint: render config templates from environment, generate a
# self-signed TLS certificate when none is mounted, wait for PostgreSQL, start Asterisk.
#
# Environment (all optional, defaults in brackets):
#   PET_API_URL            [http://pet:8000]      base URL of PET (hooks + route API)
#   PET_PBX_HOOK_SECRET    [pet]                  must equal PET's PET_PBX_HOOK_SECRET / ASTERISK_ARI_PASSWORD
#   PET_EVENTS             []                     comma separated event slugs (fallback shell contexts if PET is down)
#   PET_EMERGENCY_FALLBACK []                     number dialled when PET has no emergency route
#   PET_CDR_HOOK           [no]                   yes -> POST every CDR to the cdr hook as well
#   PET_RECORD_DIR         []                     override where announcements recorded by phone are written
#                                                 (default: the path PET returns = its PET_RECORDING_DIR,
#                                                 /var/spool/asterisk/pet-recordings; share that volume with PET)
#   DB_HOST                [db]     DB_PORT [5432]   DB_NAME [pet]      (PET's PostgreSQL - shared!)
#   DB_USER                [pet]    DB_PASSWORD [pet]                   (DATABASE_* accepted as aliases)
#   ARI_USER               [pet]    ARI_PASSWORD  [pet]   must match PET's ASTERISK_ARI_USER/PASSWORD
#   AMI_USER               [pet]    AMI_PASSWORD  [pet]   must match PET's ASTERISK_AMI_USER/PASSWORD
#   SIP_DOMAIN             [pet.local]            default_realm for digest auth
#   EXTERNAL_IP            []                     public/host IP for NAT'd SIP/RTP (docker bridge networks;
#                                                 SIP_EXTERNAL_IP accepted as alias)
#   RTP_START / RTP_END    [10000 / 10200]
#   TLS_CERT_CN            [$SIP_DOMAIN]          CN of the self-signed certificate
set -eu

: "${PET_API_URL:=http://pet:8000}"
: "${PET_PBX_HOOK_SECRET:=pet}"
: "${PET_EVENTS:=}"
: "${PET_EMERGENCY_FALLBACK:=}"
: "${PET_CDR_HOOK:=no}"
: "${PET_RECORD_DIR:=}"
: "${DB_HOST:=${DATABASE_HOST:-db}}"
: "${DB_PORT:=${DATABASE_PORT:-5432}}"
: "${DB_NAME:=${DATABASE_NAME:-pet}}"
: "${DB_USER:=${DATABASE_USER:-pet}}"
: "${DB_PASSWORD:=${DATABASE_PASSWORD:-pet}}"
: "${ARI_USER:=pet}"
: "${ARI_PASSWORD:=pet}"
: "${AMI_USER:=pet}"
: "${AMI_PASSWORD:=pet}"
: "${SIP_DOMAIN:=pet.local}"
: "${EXTERNAL_IP:=${SIP_EXTERNAL_IP:-}}"
: "${RTP_START:=10000}"
: "${RTP_END:=10200}"
: "${TLS_CERT_CN:=$SIP_DOMAIN}"
export PET_API_URL PET_PBX_HOOK_SECRET PET_EVENTS PET_EMERGENCY_FALLBACK PET_CDR_HOOK PET_RECORD_DIR \
       DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD \
       ARI_USER ARI_PASSWORD AMI_USER AMI_PASSWORD SIP_DOMAIN EXTERNAL_IP RTP_START RTP_END

TEMPLATES=/opt/pet/conf
TARGET=/etc/asterisk
# Only these placeholders are substituted; Asterisk's own ${EXTEN}-style variables are left alone.
VARS='${PET_API_URL} ${PET_PBX_HOOK_SECRET} ${PET_EMERGENCY_FALLBACK} ${PET_CDR_HOOK} ${DB_HOST} ${DB_PORT} ${DB_NAME} ${DB_USER} ${DB_PASSWORD} ${ARI_USER} ${ARI_PASSWORD} ${AMI_USER} ${AMI_PASSWORD} ${SIP_DOMAIN} ${RTP_START} ${RTP_END}'

echo "[pet] rendering configuration into $TARGET"
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
    echo "[pet] WARNING: PostgreSQL ODBC driver not found; realtime will not work" >&2
fi

# NAT: publish the host IP in SIP/SDP when running on a bridge network
if [ -n "$EXTERNAL_IP" ]; then
    sed -i "s|^;external_media_address=.*|external_media_address=$EXTERNAL_IP|; s|^;external_signaling_address=.*|external_signaling_address=$EXTERNAL_IP|" "$TARGET/pjsip.conf"
fi

# Environment file consumed by #exec scripts and the voicemail notify hook
cat > "$TARGET/pet.env" <<EOF
PET_API_URL='$PET_API_URL'
PET_PBX_HOOK_SECRET='$PET_PBX_HOOK_SECRET'
PET_EVENTS='$PET_EVENTS'
EOF
chmod 640 "$TARGET/pet.env"

# TLS: self-signed certificate for transport-tls / WSS unless one is mounted
KEYDIR="$TARGET/keys"
if [ ! -s "$KEYDIR/asterisk.pem" ]; then
    echo "[pet] generating self-signed TLS certificate for $TLS_CERT_CN"
    openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=$TLS_CERT_CN/O=PET" \
        -keyout "$KEYDIR/asterisk.key" -out "$KEYDIR/asterisk.crt" >/dev/null 2>&1
    cat "$KEYDIR/asterisk.key" "$KEYDIR/asterisk.crt" > "$KEYDIR/asterisk.pem"
fi
chmod 600 "$KEYDIR"/asterisk.* || true
# Announcements recorded by phone (Record() in [pet-services] record-announcement); PET imports from here
mkdir -p "${PET_RECORD_DIR:-/var/spool/asterisk/pet-recordings}"
chown -R asterisk:asterisk "$TARGET" /var/lib/asterisk /var/spool/asterisk /var/log/asterisk /var/run/asterisk 2>/dev/null || true

# Wait for PostgreSQL (realtime modules preload and want a working connection)
if command -v isql >/dev/null 2>&1; then
    i=0
    until echo "select 1" | isql -b asterisk "$DB_USER" "$DB_PASSWORD" >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 30 ]; then
            echo "[pet] WARNING: database $DB_HOST:$DB_PORT/$DB_NAME not reachable after 60s - starting anyway" >&2
            break
        fi
        echo "[pet] waiting for database ($i/30)..."
        sleep 2
    done
fi

echo "[pet] starting: $*"
exec "$@"
