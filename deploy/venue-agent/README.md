# PET venue agent

A small standalone program (one Python file, stdlib + `psycopg`) that runs **next to the Asterisk at the
venue**. It pulls the PBX configuration of *one* event from the central PET server over HTTPS and writes
it into a **local** PostgreSQL that Asterisk reads through ODBC Realtime. The venue box therefore needs no
database access to the central PET, and keeps running on the last applied snapshot when the uplink is gone.

```
central PET ──HTTPS──▶ venue-agent ──psycopg──▶ venue-db (PostgreSQL) ◀──ODBC realtime── Asterisk
   ▲                        │                       ▲  ps_contacts / cdr written by Asterisk
   └── heartbeat (version, hostname, contacts) ◀────┘
```

| File | Purpose |
|---|---|
| `pet_venue_agent.py` | the agent (`python3 pet_venue_agent.py`, `--once`, `--check`) |
| `Dockerfile`, `requirements.txt` | `python:3.12-slim` + `psycopg[binary]` |
| `pet-venue-agent.service` | systemd unit for bare-metal boxes |
| `test_agent.py` | pytest unit tests (mocked DB / HTTP / AMI) |
| `../asterisk/docker-compose.venue.example.yml`, `../asterisk/.env.venue.example` | complete compose stack: `venue-db` + `venue-agent` + `asterisk` |

## What it does, exactly

Every poll cycle (default every 15 s, the server can change that via `poll_interval`):

1. `GET {PET_URL}/api/v1/pbx/snapshot/?event={PET_EVENT}` with `If-None-Match: "<last applied version>"`.
   `304` → nothing to do, continue at step 6.
2. Map the snapshot tables onto the local schema. Unknown tables are skipped with a warning; a column the
   local table lacks triggers **one** schema refresh (`GET .../snapshot/schema/`, DDL applied), then the
   mapping is redone; a column that is still missing is dropped from the rows with a warning.
   `ps_contacts` and `cdr` are **never** part of the apply (Asterisk owns them locally), even if a
   snapshot contains them.
3. **One transaction**: for every table in the snapshot `DELETE FROM "<table>"` then
   `INSERT INTO "<table>" ("col", ...) VALUES (%s, ...)` via `executemany` (quoted identifiers, only
   columns known locally), then `COMMIT`. Either the whole snapshot is in place or nothing changed.
4. Reload Asterisk (`res_pjsip.so`, dialplan / `pbx_config.so`, `app_voicemail.so`) - over AMI when
   `AMI_HOST` is set, otherwise with the `ASTERISK_RELOAD` shell command (empty string disables).
   A failed reload is logged and reported in the heartbeat `message`, the snapshot stays applied.
5. Write `STATE_DIR/last_version` and `STATE_DIR/snapshot.json`.
6. `POST {PET_URL}/api/v1/pbx/agent/heartbeat/` with `event, version, hostname, agent_version,
   asterisk_ok, message` and the rows of the local `ps_contacts` (registration state) - **also when the
   snapshot was unchanged**, so PET shows the agent as alive. If the answer says `behind: true` the next
   poll happens after 2 s instead of the poll interval.

Errors (PET unreachable, HTTP 5xx, database down) are retried with exponential backoff
(`poll_interval`, ×2, ... capped at 5 min); the agent never exits on its own and never touches the local
tables unless a complete new snapshot is available. When PET *is* reachable but a cycle failed (e.g. the
local DB is down) the error text is sent in the heartbeat so operators see it in PET.

Logging: one `INFO` line per cycle **only when something was applied**; unchanged cycles log at `DEBUG`.
Warnings/errors as they happen. `SIGTERM`/`SIGINT` → clean exit code 0. `SIGHUP` → forget the cached
version and fetch a full snapshot on the next cycle.

**The local database serves exactly one event.** Because every table is wiped and rewritten on each
change, never point two agents (two events) at the same database, and never point this Asterisk at PET's
own database at the same time.

### Startup

1. `GET .../snapshot/schema/` and apply the DDL (`CREATE TABLE IF NOT EXISTS ...`, idempotent).
   If PET is unreachable this is skipped with a warning.
2. If `STATE_DIR/snapshot.json` exists and the local realtime tables are **empty** (fresh database,
   restored volume), the cached snapshot is re-applied and Asterisk reloaded - without talking to PET.
3. Enter the poll loop with `last_version` as `If-None-Match`, so a restart without uplink stays on
   the last snapshot and does not re-download anything once PET is back and unchanged.

### Offline behaviour

With PET unreachable the venue Asterisk keeps working with the **last applied snapshot**: SIP
registration/authentication, extension-to-extension calls (including forwarding, busy/no-answer
branches and voicemail boxes), conferences and the static services (echo, time, MOH) need nothing but
the local database. Everything that Asterisk asks PET for *at call time* over HTTPS does **not** work
offline: call groups, IVR menu options, unknown numbers / federation / breakout (route API), feature
codes, DECT claim, record-by-phone, wake-up/callback/survey hooks. Emergency numbers fall back to
`PET_EMERGENCY_FALLBACK` if set, otherwise play "no service". Details and the Asterisk-side view are in
`../asterisk/README.md`, section *Deployment modes*.

Calls made while offline are still written to the local `cdr` table, but PET does not get them: the
CDR hook is fire-and-forget and the agent does not forward CDRs (yet). The heartbeat's `contacts` do
catch up as soon as the uplink is back.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `PET_URL` | **required** | base URL of the central PET, e.g. `https://pet.example.org` (plain `http://` works but logs a loud warning) |
| `PET_EVENT` | **required** | event slug whose snapshot is mirrored |
| `PET_PBX_HOOK_SECRET` | – | the event's hook secret (`/e/<slug>/pbx/`, *PBX connection*); sent as `X-PET-PBX-Secret`. One of the two credentials is required |
| `PET_SYNC_TOKEN` | – | alternative: PET service token, sent as `Authorization: Bearer ...` (both may be set) |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | `localhost` / `5432` / `pet` / `pet` / `pet` | the **local** venue PostgreSQL - the same variables the PET Asterisk container uses (`DATABASE_*` accepted as aliases) |
| `DATABASE_URL` | – | overrides the `DB_*` variables (`postgres://user:pw@host:5432/db`) |
| `POLL_INTERVAL` | server's `poll_interval`, fallback `15` | seconds between polls; set to force a fixed value |
| `ASTERISK_RELOAD` | `asterisk -rx "module reload res_pjsip.so" && asterisk -rx "dialplan reload" && asterisk -rx "module reload app_voicemail.so"` | shell command run after an applied snapshot (bare metal). Empty string disables reloading |
| `AMI_HOST` / `AMI_PORT` / `AMI_USER` / `AMI_PASSWORD` | – / `5038` / – / – | when `AMI_HOST` is set the reload is done over AMI (`Action: Reload` per module) instead of the shell command - use this when the agent runs in its own container. The AMI user needs the `system` or `config` write privilege (the PET `manager.conf` template grants both) |
| `HTTP_TIMEOUT` | `10` | seconds per HTTP request (also the AMI socket timeout) |
| `STATE_DIR` | `/var/lib/pet-venue-agent` | where `last_version` and `snapshot.json` are kept - make it persistent |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows every cycle |
| `TLS_CA_FILE` | – | extra CA certificate (PEM) added to the system bundle, for PET behind a private CA |
| `INSECURE_SKIP_VERIFY` | `false` | disables TLS verification. Logs a loud warning. Never in production |

## Running

### Checks first

```sh
PET_URL=https://pet.example.org PET_EVENT=demo PET_PBX_HOOK_SECRET=... DB_HOST=... \
  python3 pet_venue_agent.py --check
```

prints PET reachability (schema + snapshot), database connectivity, presence and row counts of the
realtime tables and whether Asterisk can be reached (AMI `Ping` or `asterisk -rx`); exit code 1 when
something failed. `--once` runs a single cycle (cron style) and exits 0/1.

### docker compose (recommended)

`../asterisk/docker-compose.venue.example.yml` runs `venue-db` (postgres:16), `venue-agent` (this
directory, reload over AMI) and the unchanged PET `asterisk` image pointed at `venue-db` and at the
public PET URL, with `PET_CDR_HOOK=yes` so CDRs still reach PET. Paths in that file are relative to
`deploy/asterisk/`:

```sh
cd deploy/asterisk
cp .env.venue.example .env.venue && chmod 0600 .env.venue && $EDITOR .env.venue   # PET_URL, PET_EVENT, secret, passwords
docker compose -f docker-compose.venue.example.yml --env-file .env.venue up -d --build
docker compose -f docker-compose.venue.example.yml --env-file .env.venue run --rm venue-agent --check
docker compose -f docker-compose.venue.example.yml --env-file .env.venue logs -f venue-agent
```

### Bare metal (systemd)

```sh
install -d /usr/local/lib/pet-venue-agent
install -m 0755 pet_venue_agent.py /usr/local/lib/pet-venue-agent/
python3 -m venv /usr/local/lib/pet-venue-agent/venv && /usr/local/lib/pet-venue-agent/venv/bin/pip install 'psycopg[binary]'
useradd --system --home-dir /var/lib/pet-venue-agent pet-venue-agent
install -m 0600 -o root -g root .env.venue /etc/pet-venue-agent.env      # PET_URL, PET_EVENT, secret, DB_*, AMI_*
cp pet-venue-agent.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now pet-venue-agent
journalctl -fu pet-venue-agent
```

`systemctl reload pet-venue-agent` sends `SIGHUP` (full re-sync). For the reload of Asterisk either set
`AMI_HOST=127.0.0.1` (+ `AMI_USER`/`AMI_PASSWORD`) or keep the default `asterisk -rx` command and give
the service user access to the Asterisk control socket (`SupplementaryGroups=asterisk` in the unit).

## Security notes

- The hook secret grants **read access to the event's SIP credentials** (`ps_auths.password`) and to
  every PET hook of that event. Keep it in an env file with mode `0600` (`.env.venue`,
  `/etc/pet-venue-agent.env`), never in the compose file or shell history. Rotate it in PET
  (`/e/<slug>/pbx/`) when a venue box is lost.
- **TLS is required** in production: `PET_URL` must be `https://`. Use `TLS_CA_FILE` for a private CA
  instead of `INSECURE_SKIP_VERIFY`.
- The agent only needs `SELECT/INSERT/DELETE` on the realtime tables plus `CREATE TABLE` for the schema
  step; the local database should not be reachable from the event LAN (compose keeps it on the internal
  network; on bare metal bind PostgreSQL to `127.0.0.1`).
- `snapshot.json` in `STATE_DIR` contains the SIP passwords as well - it lives in a root/agent-owned
  directory (the Docker volume, or `StateDirectory=` with mode `0755` owned by the service user; tighten
  to `0700` if other users have shell access on the box).

## Tests

The repository's `pytest` configuration only collects `apps/` and `tests/`, so run these explicitly
from the repository root:

```sh
.venv/bin/python -m pytest -p no:cacheprovider -p no:warnings -o addopts="" -q deploy/venue-agent
.venv/bin/ruff check deploy/venue-agent
```

The tests use `unittest.mock` fakes for the database connection, the HTTP client and the AMI socket -
no PostgreSQL, network or Asterisk required.
