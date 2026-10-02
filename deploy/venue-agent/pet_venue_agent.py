#!/usr/bin/env python3
"""PET venue agent - mirrors one event's PBX configuration into a *local* PostgreSQL for Asterisk Realtime.

    GET  {PET_URL}/api/v1/pbx/snapshot/?event=<slug>         (If-None-Match: "<applied version>" -> 304)
    GET  {PET_URL}/api/v1/pbx/snapshot/schema/?event=<slug>  (DDL, applied idempotently at startup)
    POST {PET_URL}/api/v1/pbx/agent/heartbeat/               (every cycle, carries the local ps_contacts)

Cycle: fetch snapshot -> ONE transaction (DELETE + INSERT per table) -> COMMIT -> reload Asterisk
-> persist state -> heartbeat -> sleep.  Only stdlib + psycopg 3.  See README.md next to this file.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

try:
    import psycopg
except ImportError:  # pragma: no cover - reported by main()
    psycopg = None

AGENT_VERSION = "1.0.0"
DEFAULT_POLL_INTERVAL = 15
MAX_BACKOFF = 300
DEFAULT_STATE_DIR = "/var/lib/pet-venue-agent"
# Written by Asterisk itself (registrations, call records) - the snapshot never replaces them.
PROTECTED_TABLES = frozenset({"ps_contacts", "cdr"})
REALTIME_TABLES = ("ps_endpoints", "ps_auths", "ps_aors", "ps_contacts", "ps_endpoint_id_ips", "extensions",
                   "voicemail_users", "cdr")
DEFAULT_RELOAD_CMD = (
    'asterisk -rx "module reload res_pjsip.so" && asterisk -rx "dialplan reload" '
    '&& asterisk -rx "module reload app_voicemail.so"'
)
# AMI equivalents of the CLI commands above ("dialplan reload" == Reload of pbx_config.so)
AMI_RELOAD_MODULES = ("res_pjsip.so", "pbx_config.so", "app_voicemail.so")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

log = logging.getLogger("pet.venue_agent")


# ----------------------------------------------------------------------------- errors
class AgentError(Exception):
    """Base class; every failure the main loop retries with backoff."""


class ConfigError(AgentError):
    pass


class PetHTTPError(AgentError):
    def __init__(self, status: int | None, url: str, detail: str = ""):
        self.status, self.url, self.detail = status, url, detail
        where = f"HTTP {status}" if status else "connection failed"
        super().__init__(f"{where} for {url}" + (f": {detail}" if detail else ""))


class NotModified(Exception):
    """Server answered 304 - the applied snapshot is current."""


class AMIError(AgentError):
    pass


class ReloadError(AgentError):
    pass


# ----------------------------------------------------------------------------- config
def _env_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _conninfo_value(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_conninfo(env: Mapping[str, str]) -> str:
    """libpq keyword/value string from DB_* (DATABASE_* accepted as aliases like the Asterisk entrypoint)."""

    def pick(name: str, default: str) -> str:
        return env.get(f"DB_{name}") or env.get(f"DATABASE_{name}") or default

    parts = {
        "host": pick("HOST", "localhost"),
        "port": pick("PORT", "5432"),
        "dbname": pick("NAME", "pet"),
        "user": pick("USER", "pet"),
        "password": pick("PASSWORD", "pet"),
        "connect_timeout": "5",
        "application_name": f"pet-venue-agent/{AGENT_VERSION}",
    }
    return " ".join(f"{k}={_conninfo_value(v)}" for k, v in parts.items())


@dataclass
class Config:
    pet_url: str
    event: str
    hook_secret: str = ""
    sync_token: str = ""
    conninfo: str = ""
    poll_interval: int | None = None  # None -> follow the server's poll_interval
    reload_cmd: str = DEFAULT_RELOAD_CMD
    ami_host: str = ""
    ami_port: int = 5038
    ami_user: str = ""
    ami_password: str = ""
    http_timeout: float = 10.0
    state_dir: Path = field(default_factory=lambda: Path(DEFAULT_STATE_DIR))
    log_level: str = "INFO"
    tls_ca_file: str = ""
    insecure_skip_verify: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        pet_url = env.get("PET_URL", "").strip().rstrip("/")
        event = env.get("PET_EVENT", "").strip()
        missing = [name for name, value in (("PET_URL", pet_url), ("PET_EVENT", event)) if not value]
        if missing:
            raise ConfigError("missing required environment variable(s): " + ", ".join(missing))
        if not pet_url.startswith(("http://", "https://")):
            raise ConfigError("PET_URL must start with https:// (or http:// for lab setups)")
        hook_secret = env.get("PET_PBX_HOOK_SECRET", "").strip()
        sync_token = env.get("PET_SYNC_TOKEN", "").strip()
        if not hook_secret and not sync_token:
            raise ConfigError("set PET_PBX_HOOK_SECRET (event hook secret) or PET_SYNC_TOKEN (service token)")
        poll_raw = env.get("POLL_INTERVAL", "").strip()
        try:
            poll_interval = max(1, int(poll_raw)) if poll_raw else None
            ami_port = int(env.get("AMI_PORT") or 5038)
            http_timeout = float(env.get("HTTP_TIMEOUT") or 10)
        except ValueError as exc:
            raise ConfigError(f"POLL_INTERVAL / AMI_PORT / HTTP_TIMEOUT must be numbers: {exc}")
        return cls(
            pet_url=pet_url,
            event=event,
            hook_secret=hook_secret,
            sync_token=sync_token,
            conninfo=env.get("DATABASE_URL", "").strip() or build_conninfo(env),
            poll_interval=poll_interval,
            reload_cmd=env.get("ASTERISK_RELOAD", DEFAULT_RELOAD_CMD),
            ami_host=env.get("AMI_HOST", "").strip(),
            ami_port=ami_port,
            ami_user=env.get("AMI_USER", "").strip(),
            ami_password=env.get("AMI_PASSWORD", ""),
            http_timeout=http_timeout,
            state_dir=Path(env.get("STATE_DIR") or DEFAULT_STATE_DIR),
            log_level=(env.get("LOG_LEVEL") or "INFO").upper(),
            tls_ca_file=env.get("TLS_CA_FILE", "").strip(),
            insecure_skip_verify=_env_bool(env.get("INSECURE_SKIP_VERIFY")),
        )


# ----------------------------------------------------------------------------- HTTP client
def build_ssl_context(cfg: Config) -> ssl.SSLContext:
    context = ssl.create_default_context()  # system CA bundle ...
    if cfg.tls_ca_file:
        context.load_verify_locations(cafile=cfg.tls_ca_file)  # ... plus the venue's own CA
    if cfg.insecure_skip_verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


class PetClient:
    """urllib based client for the three agent endpoints."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=build_ssl_context(cfg)))

    # separated so tests can replace the transport
    def _open(self, request: urllib.request.Request, timeout: float):
        return self._opener.open(request, timeout=timeout)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": f"pet-venue-agent/{AGENT_VERSION}"}
        if self.cfg.hook_secret:
            headers["X-PET-PBX-Secret"] = self.cfg.hook_secret
        if self.cfg.sync_token:
            headers["Authorization"] = f"Bearer {self.cfg.sync_token}"
        return headers

    def request(self, method: str, path: str, *, query: dict | None = None, body: dict | None = None,
                headers: dict | None = None) -> tuple[int, dict]:
        url = self.cfg.pet_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body, default=str).encode() if body is not None else None
        all_headers = self._headers()
        all_headers.update(headers or {})
        if data is not None:
            all_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=all_headers)
        try:
            with self._open(request, self.cfg.http_timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                raise NotModified()
            detail = exc.read()[:200].decode("utf-8", "replace") if exc.fp else str(exc.reason)
            raise PetHTTPError(exc.code, url, detail.strip())
        except (urllib.error.URLError, OSError) as exc:  # includes socket.timeout / TimeoutError
            raise PetHTTPError(None, url, str(getattr(exc, "reason", exc)))
        if not raw:
            return status, {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise PetHTTPError(status, url, f"invalid JSON: {exc}")
        if not isinstance(payload, dict):
            raise PetHTTPError(status, url, "unexpected JSON payload (not an object)")
        return status, payload

    def fetch_snapshot(self, current_version: str) -> dict:
        """Full snapshot dict; raises NotModified when the server confirms ``current_version``."""
        headers = {"If-None-Match": f'"{current_version}"'} if current_version else {}
        _, payload = self.request("GET", "/api/v1/pbx/snapshot/", query={"event": self.cfg.event}, headers=headers)
        if "tables" not in payload or "version" not in payload:
            raise PetHTTPError(200, self.cfg.pet_url + "/api/v1/pbx/snapshot/", "snapshot without tables/version")
        return payload

    def fetch_schema(self) -> str:
        _, payload = self.request("GET", "/api/v1/pbx/snapshot/schema/", query={"event": self.cfg.event})
        dialect = payload.get("dialect", "postgresql")
        if dialect != "postgresql":
            raise PetHTTPError(200, self.cfg.pet_url, f"schema dialect {dialect!r} is not postgresql")
        return payload.get("sql") or ""

    def heartbeat(self, payload: dict) -> dict:
        _, response = self.request("POST", "/api/v1/pbx/agent/heartbeat/", body=payload)
        return response


# ----------------------------------------------------------------------------- state on disk
class State:
    """``last_version`` + ``snapshot.json`` so a restart without PET still knows what it runs."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.version_file = self.dir / "last_version"
        self.snapshot_file = self.dir / "snapshot.json"

    @property
    def version(self) -> str:
        try:
            return self.version_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def load_snapshot(self) -> dict | None:
        try:
            data = json.loads(self.snapshot_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and "tables" in data else None

    def save(self, snapshot: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        for path, text in ((self.snapshot_file, json.dumps(snapshot, default=str)),
                           (self.version_file, str(snapshot.get("version", "")) + "\n")):
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)


# ----------------------------------------------------------------------------- database
def quote_ident(name: str) -> str:
    if not IDENT_RE.match(name):
        raise AgentError(f"refusing to use identifier {name!r}")
    return '"' + name.replace('"', '""') + '"'


@dataclass
class TablePlan:
    table: str
    columns: list[str]
    rows: list[tuple]

    def delete_sql(self) -> str:
        return f"DELETE FROM {quote_ident(self.table)}"

    def insert_sql(self) -> str:
        cols = ", ".join(quote_ident(c) for c in self.columns)
        marks = ", ".join(["%s"] * len(self.columns))
        return f"INSERT INTO {quote_ident(self.table)} ({cols}) VALUES ({marks})"


def plan_tables(tables: Mapping[str, dict], local_columns: Mapping[str, list[str]]
                ) -> tuple[list[TablePlan], dict[str, list[str]], list[str]]:
    """Map snapshot tables onto the local schema.

    Returns ``(plans, missing_columns, unknown_tables)``: protected tables are dropped with a warning,
    tables absent locally are reported in ``unknown_tables`` and columns absent locally in
    ``missing_columns`` (the caller decides whether to refresh the schema first).
    """
    plans: list[TablePlan] = []
    missing: dict[str, list[str]] = {}
    unknown: list[str] = []
    for name, spec in tables.items():
        if name in PROTECTED_TABLES:
            log.warning("snapshot contains protected table %s - ignored (Asterisk owns it locally)", name)
            continue
        if not IDENT_RE.match(name) or name not in local_columns:
            unknown.append(name)
            continue
        rows = (spec or {}).get("rows") or []
        wanted: list[str] = []
        for row in rows:
            for column in row:
                if column not in wanted:
                    wanted.append(column)
        local = set(local_columns[name])
        keep = [c for c in wanted if c in local and IDENT_RE.match(c)]
        drop = [c for c in wanted if c not in keep]
        if drop:
            missing[name] = drop
        values = [tuple(row.get(c) for c in keep) for row in rows] if keep else []
        plans.append(TablePlan(name, keep, values))
    return plans, missing, unknown


def connect_db(conninfo: str):
    if psycopg is None:  # pragma: no cover
        raise AgentError("psycopg is not installed (pip install 'psycopg[binary]')")
    # autocommit: DDL / SELECTs never leave an idle transaction open; the apply uses an explicit block
    return psycopg.connect(conninfo, autocommit=True)


def fetch_columns(conn) -> dict[str, list[str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() ORDER BY table_name, ordinal_position"
        )
        columns: dict[str, list[str]] = {}
        for table, column in cur.fetchall():
            columns.setdefault(table, []).append(column)
    return columns


def apply_schema(conn, sql: str) -> None:
    if not sql.strip():
        return
    with conn.cursor() as cur:
        cur.execute(sql)


def execute_plan(conn, plans: list[TablePlan]) -> None:
    """DELETE + INSERT for every table in *one* transaction."""
    with conn.transaction():
        with conn.cursor() as cur:
            for plan in plans:
                cur.execute(plan.delete_sql())
                if plan.rows:
                    cur.executemany(plan.insert_sql(), plan.rows)


def row_counts(conn, tables: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(f"SELECT count(*) FROM {quote_ident(table)}")
            counts[table] = int(cur.fetchone()[0])
    return counts


def fetch_contacts(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute('SELECT * FROM "ps_contacts"')
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row, strict=False)) for row in cur.fetchall()]


# ----------------------------------------------------------------------------- Asterisk reload
class AMI:
    """Just enough Asterisk Manager Interface for Login / Reload / Ping / Logoff (plain TCP socket)."""

    def __init__(self, host: str, port: int = 5038, user: str = "", password: str = "", timeout: float = 5.0,
                 connect=socket.create_connection):
        self.host, self.port, self.user, self.password, self.timeout = host, port, user, password, timeout
        self._connect = connect
        self.sock = None
        self._buf = b""
        self._seq = 0

    def __enter__(self) -> AMI:
        try:
            self.sock = self._connect((self.host, self.port), timeout=self.timeout)
            self._read_line()  # banner "Asterisk Call Manager/x.y"
            response = self.action("Login", Username=self.user, Secret=self.password)
        except OSError as exc:
            self.close()
            raise AMIError(f"AMI connect to {self.host}:{self.port} failed: {exc}")
        if response.get("Response") != "Success":
            self.close()
            raise AMIError(f"AMI login failed: {response.get('Message', 'unknown error')}")
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.sock is not None:
                self.action("Logoff")
        except Exception:  # noqa: BLE001 - best effort
            pass
        self.close()

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _read_line(self) -> str:
        if self.sock is None:
            raise AMIError("AMI not connected")
        while b"\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise AMIError("AMI connection closed")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\r\n")
        return line.decode("utf-8", "replace")

    def _read_packet(self) -> dict[str, str]:
        packet: dict[str, str] = {}
        while True:
            line = self._read_line()
            if line == "":
                if packet:
                    return packet
                continue
            key, sep, value = line.partition(":")
            if sep:
                packet[key.strip()] = value.strip()

    def action(self, name: str, **params: str) -> dict[str, str]:
        if self.sock is None:
            raise AMIError("AMI not connected")
        self._seq += 1
        action_id = f"pva{self._seq}"
        lines = [f"Action: {name}", f"ActionID: {action_id}"] + [f"{k}: {v}" for k, v in params.items()]
        try:
            self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        except OSError as exc:
            raise AMIError(f"AMI send failed: {exc}")
        while True:  # unsolicited events are skipped
            packet = self._read_packet()
            if packet.get("ActionID") == action_id and "Response" in packet:
                return packet

    def reload(self, module: str) -> bool:
        return self.action("Reload", Module=module).get("Response") == "Success"

    def ping(self) -> bool:
        return self.action("Ping").get("Response") == "Success"


class Reloader:
    """Reload Asterisk after a snapshot: AMI when AMI_HOST is set, otherwise the ASTERISK_RELOAD shell command."""

    def __init__(self, cfg: Config, ami_factory=AMI, run=subprocess.run, which=shutil.which):
        self.cfg = cfg
        self._ami_factory = ami_factory
        self._run = run
        self._which = which

    @property
    def mode(self) -> str:
        if self.cfg.ami_host:
            return "ami"
        return "cli" if self.cfg.reload_cmd.strip() else "disabled"

    def _ami(self) -> AMI:
        cfg = self.cfg
        return self._ami_factory(cfg.ami_host, cfg.ami_port, cfg.ami_user, cfg.ami_password, timeout=cfg.http_timeout)

    def reload(self) -> str:
        """Returns a short description of what was done; raises ReloadError."""
        mode = self.mode
        if mode == "disabled":
            return "reload disabled"
        if mode == "ami":
            try:
                with self._ami() as ami:
                    failed = [m for m in AMI_RELOAD_MODULES if not ami.reload(m)]
            except AMIError as exc:
                raise ReloadError(str(exc))
            if failed:
                raise ReloadError("AMI Reload failed for " + ", ".join(failed))
            return "AMI reload of " + ", ".join(AMI_RELOAD_MODULES)
        try:
            result = self._run(self.cfg.reload_cmd, shell=True, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReloadError(f"reload command failed: {exc}")
        if result.returncode != 0:
            output = (result.stderr or result.stdout or "").strip().splitlines()
            raise ReloadError(f"reload command exited {result.returncode}: {output[-1] if output else 'no output'}")
        return "reload command ok"

    def probe(self) -> bool | None:
        """True/False when Asterisk can be probed (AMI Ping or ``asterisk -rx``), None when it cannot."""
        if self.mode == "ami":
            try:
                with self._ami() as ami:
                    return ami.ping()
            except AMIError:
                return False
        if self._which("asterisk"):
            try:
                result = self._run(["asterisk", "-rx", "core show version"], capture_output=True, text=True,
                                   timeout=15)
            except (OSError, subprocess.SubprocessError):
                return False
            return result.returncode == 0
        return None


# ----------------------------------------------------------------------------- backoff
class Backoff:
    """base, 2*base, 4*base ... capped at ``cap`` seconds; ``reset()`` after a success."""

    def __init__(self, base: float, cap: float = MAX_BACKOFF):
        self.base, self.cap, self.failures = max(1.0, float(base)), float(cap), 0

    def next_delay(self) -> float:
        delay = min(self.cap, self.base * (2 ** self.failures))
        self.failures += 1
        return delay

    def reset(self) -> None:
        self.failures = 0


# ----------------------------------------------------------------------------- the agent
class Agent:
    def __init__(self, cfg: Config, client: PetClient | None = None, connect=None, reloader: Reloader | None = None,
                 hostname: str | None = None):
        self.cfg = cfg
        self.client = client or PetClient(cfg)
        self.state = State(cfg.state_dir)
        self.reloader = reloader or Reloader(cfg)
        self._connect = connect or (lambda: connect_db(cfg.conninfo))
        self.hostname = hostname or socket.gethostname()
        self.conn = None
        self.columns: dict[str, list[str]] | None = None
        self.poll_interval = cfg.poll_interval or DEFAULT_POLL_INTERVAL
        self.last_error = ""
        self.asterisk_ok: bool | None = None
        self.behind = False
        self.force_full = False
        self.stop = threading.Event()

    # --- helpers -----------------------------------------------------------------
    def db(self):
        if self.conn is None or getattr(self.conn, "closed", False):
            self.conn = self._connect()
            self.columns = None
        return self.conn

    def _drop_connection(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
        self.conn, self.columns = None, None

    def local_columns(self, refresh: bool = False) -> dict[str, list[str]]:
        if self.columns is None or refresh:
            self.columns = fetch_columns(self.db())
        return self.columns

    def _set_poll_interval(self, value) -> None:
        if self.cfg.poll_interval is None and isinstance(value, int) and value > 0:
            self.poll_interval = value

    def ensure_schema(self) -> None:
        sql = self.client.fetch_schema()
        apply_schema(self.db(), sql)
        self.local_columns(refresh=True)
        log.debug("schema applied (%d statement(s))", sql.count(";"))

    # --- snapshot ----------------------------------------------------------------
    def apply_snapshot(self, snapshot: dict) -> dict[str, int]:
        """DELETE+INSERT every snapshot table in one transaction; returns rows written per table."""
        if snapshot.get("event") not in (None, "", self.cfg.event):
            raise AgentError(f"snapshot is for event {snapshot.get('event')!r}, expected {self.cfg.event!r}")
        tables = snapshot.get("tables") or {}
        plans, missing, unknown = plan_tables(tables, self.local_columns())
        if missing or unknown:
            log.info("snapshot needs schema not present locally (columns %s, tables %s) - refreshing schema once",
                     missing or "-", unknown or "-")
            try:
                self.ensure_schema()
            except AgentError as exc:
                log.warning("schema refresh failed: %s", exc)
                self.local_columns(refresh=True)
            plans, missing, unknown = plan_tables(tables, self.local_columns())
            for table, cols in missing.items():
                log.warning("dropping column(s) %s from %s: not in local schema", ", ".join(cols), table)
        for table in unknown:
            log.warning("unknown table %s in snapshot - skipped", table)
        execute_plan(self.db(), plans)
        return {plan.table: len(plan.rows) for plan in plans}

    def activate(self, snapshot: dict) -> str:
        """apply -> reload Asterisk -> persist state. Returns a one-line summary; reload problems are recorded."""
        stats = self.apply_snapshot(snapshot)
        self._set_poll_interval(snapshot.get("poll_interval"))
        reload_msg = self._reload()
        self.state.save(snapshot)
        rows = ", ".join(f"{t}={n}" for t, n in stats.items())
        return (f"applied {str(snapshot.get('version', ''))[:12]} ({rows}) "
                f"generated_at={snapshot.get('generated_at', '?')}; {reload_msg}")

    def _reload(self) -> str:
        try:
            message = self.reloader.reload()
        except ReloadError as exc:
            self.last_error = f"asterisk reload: {exc}"
            self.asterisk_ok = False
            log.error("Asterisk reload failed: %s", exc)
            return "asterisk reload FAILED"
        self.last_error = ""
        if self.reloader.mode != "disabled":
            self.asterisk_ok = True
        return message

    # --- heartbeat ---------------------------------------------------------------
    def heartbeat_payload(self) -> dict:
        contacts: list[dict] = []
        try:
            contacts = fetch_contacts(self.db())
        except Exception as exc:  # noqa: BLE001 - table missing / DB down: the heartbeat still goes out
            log.debug("could not read ps_contacts: %s", exc)
        probe = self.reloader.probe()
        if probe is not None:
            self.asterisk_ok = probe
        return {
            "event": self.cfg.event,
            "version": self.state.version,
            "hostname": self.hostname,
            "agent_version": AGENT_VERSION,
            # unknown (no reload attempted, no probe possible) counts as ok - we have no evidence of a problem
            "asterisk_ok": self.asterisk_ok is not False,
            "message": self.last_error,
            "contacts": contacts,
        }

    def heartbeat(self) -> dict | None:
        try:
            response = self.client.heartbeat(self.heartbeat_payload())
        except PetHTTPError as exc:
            log.warning("heartbeat failed: %s", exc)
            return None
        self._set_poll_interval(response.get("poll_interval"))
        self.behind = bool(response.get("behind"))
        if self.behind:
            log.info("PET reports current version %s, we run %s - fetching next cycle",
                     str(response.get("current_version", ""))[:12], self.state.version[:12] or "<none>")
        return response

    # --- lifecycle ---------------------------------------------------------------
    def startup(self) -> None:
        if not self.cfg.pet_url.startswith("https://"):
            log.warning("PET_URL is plain http:// - the hook secret and SIP credentials travel unencrypted!")
        if self.cfg.insecure_skip_verify:
            log.warning("INSECURE_SKIP_VERIFY is set - TLS certificates are NOT verified. Do not use in production.")
        try:
            self.ensure_schema()
        except Exception as exc:  # noqa: BLE001 - DB or PET unreachable: keep going with what we have
            log.warning("startup: could not apply schema (%s) - continuing", exc)
        cached = self.state.load_snapshot()
        if cached is None:
            log.info("no cached snapshot in %s - waiting for PET", self.state.dir)
            return
        try:
            names = [t for t in cached.get("tables", {}) if t not in PROTECTED_TABLES and t in self.local_columns()]
            counts = row_counts(self.db(), names)
        except Exception as exc:  # noqa: BLE001
            log.warning("startup: cannot inspect local tables (%s)", exc)
            return
        cached_rows = sum(len((cached["tables"][t] or {}).get("rows") or []) for t in names)
        if cached_rows and not any(counts.values()):
            log.info("local database is empty - re-applying cached snapshot %s", str(cached.get("version"))[:12])
            try:
                log.info("startup: %s", self.activate(cached))
            except Exception as exc:  # noqa: BLE001
                log.error("re-applying cached snapshot failed: %s", exc)
        else:
            log.info("starting with snapshot %s (%d row(s) in local realtime tables)",
                     self.state.version[:12] or "<none>", sum(counts.values()))

    def cycle(self) -> bool:
        """One poll: snapshot (or 304) -> apply -> heartbeat. Returns True when something changed."""
        current = "" if self.force_full else self.state.version
        self.force_full = False
        try:
            snapshot = self.client.fetch_snapshot(current)
        except NotModified:
            snapshot = None
        # reaching this point means PET and (below) the DB work again - only a reload failure is sticky
        if not self.last_error.startswith("asterisk reload"):
            self.last_error = ""
        summary = ""
        if snapshot is not None:
            if current and snapshot.get("version") == current:
                log.debug("server sent the version we already run (%s)", current[:12])
            else:
                summary = self.activate(snapshot)
            self._set_poll_interval(snapshot.get("poll_interval"))
        heartbeat = "heartbeat ok" if self.heartbeat() is not None else "heartbeat FAILED"
        if summary:
            log.info("%s; %s; next poll in %ss", summary, heartbeat, self.poll_interval)
        else:
            log.debug("unchanged (%s); %s; next poll in %ss", self.state.version[:12] or "<none>", heartbeat,
                      self.poll_interval)
        return bool(summary)

    def run(self, once: bool = False) -> int:
        self.startup()
        backoff = Backoff(self.poll_interval)
        while not self.stop.is_set():
            try:
                self.cycle()
            except Exception as exc:  # noqa: BLE001 - never die: keep serving the last applied snapshot
                self._on_failure(exc)
                if once:
                    return 1
                delay = backoff.next_delay()
                what = str(exc) if isinstance(exc, AgentError) else f"{type(exc).__name__}: {exc}"
                log.warning("cycle failed: %s - retrying in %.0fs", what, delay)
            else:
                backoff.reset()
                if once:
                    return 0
                delay = 2 if self.behind else self.poll_interval
            self.stop.wait(delay)
        log.info("stopped")
        return 0

    def _on_failure(self, exc: Exception) -> None:
        self.last_error = str(exc)[:500]
        if not isinstance(exc, PetHTTPError):
            self._drop_connection()  # DB errors: reconnect next time
        if not (isinstance(exc, PetHTTPError) and exc.status is None):
            self.heartbeat()  # PET is reachable: let it show the error

    # --- --check -----------------------------------------------------------------
    def check(self, out=sys.stdout) -> int:
        ok = True

        def line(label: str, good: bool | None, text: str) -> None:
            nonlocal ok
            mark = "OK  " if good else ("--  " if good is None else "FAIL")
            if good is False:
                ok = False
            print(f"[{mark}] {label}: {text}", file=out)

        line("agent", None, f"pet-venue-agent {AGENT_VERSION} on {self.hostname}, event {self.cfg.event}")
        line("state", None, f"{self.state.dir} version={self.state.version or '<none>'} "
                            f"cached_snapshot={'yes' if self.state.load_snapshot() else 'no'}")
        try:
            schema_sql = self.client.fetch_schema()
            line("PET schema", True, f"{self.cfg.pet_url} ({schema_sql.count(';')} statement(s))")
        except AgentError as exc:
            line("PET schema", False, str(exc))
        try:
            snapshot = self.client.fetch_snapshot("")
            tables = snapshot.get("tables") or {}
            rows = ", ".join(f"{t}={len((s or {}).get('rows') or [])}" for t, s in tables.items())
            line("PET snapshot", True, f"version {str(snapshot.get('version'))[:12]} "
                                       f"poll_interval={snapshot.get('poll_interval')} ({rows})")
        except NotModified:
            line("PET snapshot", True, "304 not modified")
        except AgentError as exc:
            line("PET snapshot", False, str(exc))
        try:
            conn = self.db()
            columns = self.local_columns(refresh=True)
            line("database", True, f"connected ({len(columns)} table(s) in current schema)")
            present = [t for t in REALTIME_TABLES if t in columns]
            absent = [t for t in REALTIME_TABLES if t not in columns]
            counts = row_counts(conn, present)
            line("realtime tables", not absent, ", ".join(f"{t}={n}" for t, n in counts.items()) +
                 (f"; MISSING: {', '.join(absent)}" if absent else ""))
        except Exception as exc:  # noqa: BLE001
            line("database", False, f"{type(exc).__name__}: {exc}")
        probe = self.reloader.probe()
        line("asterisk", probe, f"reload mode {self.reloader.mode}, "
                                f"probe {'ok' if probe else ('failed' if probe is False else 'not possible')}")
        return 0 if ok else 1


# ----------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PET venue agent: mirror PBX snapshots into a local PostgreSQL")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit (cron style)")
    parser.add_argument("--check", action="store_true", help="print connectivity / database / schema status and exit")
    parser.add_argument("--version", action="version", version=f"pet-venue-agent {AGENT_VERSION}")
    args = parser.parse_args(argv)

    level = (os.environ.get("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    logging.getLogger().setLevel(getattr(logging, cfg.log_level, logging.INFO))
    if psycopg is None:
        log.error("psycopg is not installed - pip install 'psycopg[binary]'")
        return 2
    agent = Agent(cfg)
    if args.check:
        return agent.check()

    def _stop(signum, _frame):
        log.info("signal %s received - shutting down", signal.Signals(signum).name)
        agent.stop.set()

    def _resync(_signum, _frame):
        log.info("SIGHUP received - forcing a full re-sync on the next cycle")
        agent.force_full = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _resync)
    return agent.run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
