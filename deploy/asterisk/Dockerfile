# PET reference Asterisk (Ubuntu 24.04 noble, Asterisk 20.x from the distro - Debian bookworm no longer ships asterisk).
# Everything is installed at build time - the container never downloads anything at runtime.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        asterisk asterisk-modules asterisk-config \
        asterisk-core-sounds-en-gsm asterisk-moh-opsound-gsm \
        odbc-postgresql unixodbc \
        curl ca-certificates openssl gettext-base jq procps \
    && rm -rf /var/lib/apt/lists/*
# The distro sample configs (asterisk-config) stay in /etc/asterisk as defaults for everything PET does
# not template (indications.conf, features.conf, codecs.conf, pjproject.conf, ...); entrypoint.sh
# overwrites the PET-managed files from /opt/pet/conf at start.

# Config templates (rendered into /etc/asterisk by entrypoint.sh) and helper scripts
COPY conf/ /opt/pet/conf/
COPY scripts/ /opt/pet/scripts/
COPY entrypoint.sh /opt/pet/entrypoint.sh
RUN chmod +x /opt/pet/entrypoint.sh /opt/pet/scripts/*.sh \
    && mkdir -p /etc/asterisk/keys /var/lib/asterisk/sounds/pet /var/spool/asterisk/voicemail /var/run/asterisk \
    && chown -R asterisk:asterisk /etc/asterisk /var/lib/asterisk /var/spool/asterisk /var/log/asterisk /var/run/asterisk

# SIP (UDP/TCP/TLS), HTTP/ARI, WSS, AMI, RTP
EXPOSE 5060/udp 5060/tcp 5061/tcp 8088/tcp 8089/tcp 5038/tcp 10000-10200/udp

VOLUME ["/var/spool/asterisk/voicemail", "/var/lib/asterisk/sounds/pet", "/etc/asterisk/keys"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS -u "${ARI_USER:-pet}:${ARI_PASSWORD:-pet}" http://127.0.0.1:8088/ari/asterisk/info >/dev/null || exit 1

ENTRYPOINT ["/opt/pet/entrypoint.sh"]
CMD ["asterisk", "-f", "-U", "asterisk", "-G", "asterisk", "-vvv"]
