# DIAL venue agent - mirrors one event's PBX snapshot into a local PostgreSQL for Asterisk Realtime.
# Only stdlib + psycopg; nothing is downloaded at runtime.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STATE_DIR=/var/lib/dial-venue-agent

COPY requirements.txt /opt/dial-venue-agent/requirements.txt
RUN pip install -r /opt/dial-venue-agent/requirements.txt \
    && useradd --system --uid 900 --home-dir /var/lib/dial-venue-agent --create-home dial-venue-agent

COPY dial_venue_agent.py /opt/dial-venue-agent/dial_venue_agent.py

# last_version + snapshot.json: keep this volume so a restart while DIAL is unreachable still knows its state
VOLUME ["/var/lib/dial-venue-agent"]
USER dial-venue-agent
WORKDIR /opt/dial-venue-agent

ENTRYPOINT ["python3", "/opt/dial-venue-agent/dial_venue_agent.py"]
