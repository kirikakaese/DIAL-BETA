# PET venue agent - mirrors one event's PBX snapshot into a local PostgreSQL for Asterisk Realtime.
# Only stdlib + psycopg; nothing is downloaded at runtime.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STATE_DIR=/var/lib/pet-venue-agent

COPY requirements.txt /opt/pet-venue-agent/requirements.txt
RUN pip install -r /opt/pet-venue-agent/requirements.txt \
    && useradd --system --uid 900 --home-dir /var/lib/pet-venue-agent --create-home pet-venue-agent

COPY pet_venue_agent.py /opt/pet-venue-agent/pet_venue_agent.py

# last_version + snapshot.json: keep this volume so a restart while PET is unreachable still knows its state
VOLUME ["/var/lib/pet-venue-agent"]
USER pet-venue-agent
WORKDIR /opt/pet-venue-agent

ENTRYPOINT ["python3", "/opt/pet-venue-agent/pet_venue_agent.py"]
