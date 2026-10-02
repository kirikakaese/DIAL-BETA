"""Tests for the PET venue agent (pytest + unittest.mock, no database / network needed).

Run from the repository root:
    .venv/bin/python -m pytest -p no:cacheprovider -p no:warnings -o addopts="" -q deploy/venue-agent
"""
from __future__ import annotations

import io
import json
import logging
import re
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pet_venue_agent as agent_mod
import pytest
from pet_venue_agent import (
    AMI,
    Agent,
    Backoff,
    Config,
    NotModified,
    PetClient,
    PetHTTPError,
    Reloader,
    ReloadError,
    plan_tables,
)

LOCAL_COLUMNS = {
    "ps_endpoints": ["id", "transport", "aors", "auth", "context", "disallow", "allow", "callerid", "set_var"],
    "ps_auths": ["id", "auth_type", "username", "password"],
    "ps_aors": ["id", "max_contacts", "mailboxes"],
    "ps_contacts": ["id", "uri", "endpoint", "expiration_time"],
    "ps_endpoint_id_ips": ["id", "endpoint", "match"],
    "extensions": ["id", "context", "exten", "priority", "app", "appdata"],
    "voicemail_users": ["id", "context", "mailbox", "password", "fullname"],
    "cdr": ["id", "src", "dst", "start"],
}


# ----------------------------------------------------------------------------- fakes
class FakeCursor:
    def __init__(self, conn: FakeConn):
        self.conn = conn
        self.description = None
        self._result: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if "information_schema.columns" in sql:
            self._result = [(t, c) for t, cols in self.conn.columns.items() for c in cols]
        elif sql.startswith("SELECT count(*)"):
            table = re.search(r'"(\w+)"', sql).group(1)
            self._result = [(self.conn.counts.get(table, 0),)]
        elif sql.startswith('SELECT * FROM "ps_contacts"'):
            self.description = [(c,) for c in LOCAL_COLUMNS["ps_contacts"]]
            self._result = self.conn.contacts
        elif sql.lstrip().upper().startswith("CREATE TABLE"):
            self.conn.ddl_applied += 1
            if self.conn.on_ddl:
                self.conn.on_ddl(self.conn)

    def executemany(self, sql, rows):
        self.conn.executed.append((sql, list(rows)))

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0]


class FakeConn:
    """Records everything executed; ``transaction()`` records BEGIN/COMMIT markers."""

    closed = False

    def __init__(self, columns=None, counts=None, contacts=None, on_ddl=None):
        self.columns = {k: list(v) for k, v in (columns or LOCAL_COLUMNS).items()}
        self.counts = counts or {}
        self.contacts = contacts or []
        self.on_ddl = on_ddl
        self.executed: list[tuple] = []
        self.ddl_applied = 0

    def cursor(self):
        return FakeCursor(self)

    @contextmanager
    def transaction(self):
        self.executed.append(("BEGIN", None))
        yield
        self.executed.append(("COMMIT", None))

    def close(self):
        self.closed = True

    # helpers for assertions
    def statements(self, prefix: str = "") -> list[str]:
        return [sql for sql, _ in self.executed if sql.startswith(prefix)]

    def writes(self) -> list[tuple]:
        return [(sql, p) for sql, p in self.executed if sql.startswith(("DELETE", "INSERT", "BEGIN", "COMMIT"))]


def make_config(tmp_path: Path, **overrides) -> Config:
    values = {
        "pet_url": "https://pet.example.org",
        "event": "demo",
        "hook_secret": "s3cret",
        "conninfo": "host=venue-db dbname=pet",
        "state_dir": tmp_path / "state",
        "reload_cmd": "true",
    }
    values.update(overrides)
    return Config(**values)


def make_agent(tmp_path: Path, conn: FakeConn | None = None, snapshot=None, **cfg_overrides):
    cfg = make_config(tmp_path, **cfg_overrides)
    conn = conn or FakeConn()
    client = mock.MagicMock(spec=PetClient)
    client.fetch_schema.return_value = "CREATE TABLE IF NOT EXISTS ps_endpoints (id varchar(40));"
    client.heartbeat.return_value = {"ok": True, "current_version": "", "behind": False}
    if snapshot is not None:
        client.fetch_snapshot.return_value = snapshot
    reloader = mock.MagicMock(spec=Reloader)
    reloader.mode = "cli"
    reloader.reload.return_value = "reload command ok"
    reloader.probe.return_value = True
    agent = Agent(cfg, client=client, connect=lambda: conn, reloader=reloader, hostname="venue-box")
    return agent, conn, client, reloader


def sample_snapshot(version="abc123def456", **extra_tables) -> dict:
    tables = {
        "ps_endpoints": {"key": "id", "rows": [
            {"id": "demo-aaaa", "transport": "transport-udp", "aors": "demo-aaaa", "auth": "demo-aaaa",
             "context": "pet-demo", "disallow": "all", "allow": "alaw,ulaw", "callerid": None,
             "set_var": "PET_EVENT=demo"},
        ]},
        "ps_auths": {"key": "id", "rows": [
            {"id": "demo-aaaa", "auth_type": "userpass", "username": "demo-aaaa", "password": "pw"},
        ]},
        "ps_aors": {"key": "id", "rows": [{"id": "demo-aaaa", "max_contacts": 1, "mailboxes": "4242@pet-demo"}]},
        "ps_endpoint_id_ips": {"key": "id", "rows": []},
        "extensions": {"key": "id", "rows": [
            {"id": 1, "context": "pet-demo", "exten": "4242", "priority": 1, "app": "Set",
             "appdata": "__PET_EVENT=demo"},
            {"id": 2, "context": "pet-demo", "exten": "4242", "priority": 2, "app": "Dial",
             "appdata": "PJSIP/demo-aaaa,30,tT"},
        ]},
        "voicemail_users": {"key": "id", "rows": [
            {"id": 1, "context": "pet-demo", "mailbox": "4242", "password": "1234", "fullname": "Alice"},
        ]},
    }
    tables.update(extra_tables)
    return {"event": "demo", "version": version, "generated_at": "2026-09-14T10:00:00+00:00",
            "poll_interval": 20, "tables": tables}


# ----------------------------------------------------------------------------- config
def test_config_from_env_builds_conninfo_and_defaults():
    cfg = Config.from_env({
        "PET_URL": "https://pet.example.org/", "PET_EVENT": "demo", "PET_PBX_HOOK_SECRET": "x",
        "DB_HOST": "venue-db", "DB_PORT": "5433", "DB_NAME": "asterisk", "DB_USER": "ast", "DB_PASSWORD": "p'w",
        "AMI_HOST": "asterisk", "AMI_USER": "pet", "AMI_PASSWORD": "pet",
    })
    assert cfg.pet_url == "https://pet.example.org"  # trailing slash stripped
    assert cfg.poll_interval is None  # follow the server
    assert "host='venue-db'" in cfg.conninfo and "port='5433'" in cfg.conninfo
    assert "dbname='asterisk'" in cfg.conninfo and "password='p\\'w'" in cfg.conninfo
    assert cfg.reload_cmd == agent_mod.DEFAULT_RELOAD_CMD
    assert cfg.ami_host == "asterisk" and cfg.ami_port == 5038
    assert cfg.state_dir == Path("/var/lib/pet-venue-agent")


def test_config_requires_url_event_and_credentials():
    with pytest.raises(agent_mod.ConfigError, match="PET_URL, PET_EVENT"):
        Config.from_env({})
    with pytest.raises(agent_mod.ConfigError, match="PET_PBX_HOOK_SECRET"):
        Config.from_env({"PET_URL": "https://x", "PET_EVENT": "demo"})
    cfg = Config.from_env({"PET_URL": "https://x", "PET_EVENT": "demo", "PET_SYNC_TOKEN": "tok",
                           "DATABASE_URL": "postgres://a:b@c/d", "ASTERISK_RELOAD": "", "POLL_INTERVAL": "7"})
    assert cfg.conninfo == "postgres://a:b@c/d" and cfg.reload_cmd == "" and cfg.poll_interval == 7


# ----------------------------------------------------------------------------- HTTP client
class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_client_sends_auth_headers_and_if_none_match(tmp_path):
    cfg = make_config(tmp_path, sync_token="tok")
    client = PetClient(cfg)
    seen = {}

    def fake_open(request, timeout):
        seen["request"] = request
        return _Response(json.dumps({"version": "v2", "tables": {}}).encode())

    with mock.patch.object(client, "_open", side_effect=fake_open):
        snapshot = client.fetch_snapshot("v1")
    request = seen["request"]
    assert snapshot["version"] == "v2"
    assert request.full_url == "https://pet.example.org/api/v1/pbx/snapshot/?event=demo"
    assert request.get_header("X-pet-pbx-secret") == "s3cret"
    assert request.get_header("Authorization") == "Bearer tok"
    assert request.get_header("If-none-match") == '"v1"'


def test_client_translates_304_and_errors(tmp_path):
    client = PetClient(make_config(tmp_path))
    not_modified = urllib.error.HTTPError("u", 304, "Not Modified", {}, None)
    with mock.patch.object(client, "_open", side_effect=not_modified), pytest.raises(NotModified):
        client.fetch_snapshot("v1")
    forbidden = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b'{"detail":"bad secret"}'))
    with mock.patch.object(client, "_open", side_effect=forbidden), pytest.raises(PetHTTPError) as info:
        client.fetch_schema()
    assert info.value.status == 403 and "bad secret" in str(info.value)
    with mock.patch.object(client, "_open", side_effect=urllib.error.URLError("refused")), \
            pytest.raises(PetHTTPError) as info:
        client.heartbeat({})
    assert info.value.status is None


# ----------------------------------------------------------------------------- apply
def test_apply_snapshot_builds_sql_in_one_transaction(tmp_path):
    agent, conn, client, reloader = make_agent(tmp_path, snapshot=sample_snapshot())
    stats = agent.apply_snapshot(sample_snapshot())

    writes = conn.writes()
    assert writes[0][0] == "BEGIN" and writes[-1][0] == "COMMIT"
    assert writes.count(("BEGIN", None)) == 1
    deletes = [sql for sql, _ in writes if sql.startswith("DELETE")]
    assert deletes == ['DELETE FROM "ps_endpoints"', 'DELETE FROM "ps_auths"', 'DELETE FROM "ps_aors"',
                       'DELETE FROM "ps_endpoint_id_ips"', 'DELETE FROM "extensions"', 'DELETE FROM "voicemail_users"']
    inserts = {sql: rows for sql, rows in writes if sql.startswith("INSERT")}
    ep_sql = ('INSERT INTO "ps_endpoints" ("id", "transport", "aors", "auth", "context", "disallow", "allow", '
              '"callerid", "set_var") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)')
    assert inserts[ep_sql] == [("demo-aaaa", "transport-udp", "demo-aaaa", "demo-aaaa", "pet-demo", "all",
                                "alaw,ulaw", None, "PET_EVENT=demo")]
    ext_sql = ('INSERT INTO "extensions" ("id", "context", "exten", "priority", "app", "appdata") '
               'VALUES (%s, %s, %s, %s, %s, %s)')
    assert len(inserts[ext_sql]) == 2 and inserts[ext_sql][1][4] == "Dial"
    assert not any("ps_endpoint_id_ips" in sql for sql in inserts)  # empty table: DELETE only
    assert stats == {"ps_endpoints": 1, "ps_auths": 1, "ps_aors": 1, "ps_endpoint_id_ips": 0, "extensions": 2,
                     "voicemail_users": 1}
    client.fetch_schema.assert_not_called()  # everything known -> no schema refresh


def test_protected_tables_are_never_touched(tmp_path, caplog):
    snapshot = sample_snapshot(ps_contacts={"key": "id", "rows": [{"id": "x", "uri": "sip:x"}]},
                               cdr={"key": "id", "rows": [{"id": 1}]})
    agent, conn, _, _ = make_agent(tmp_path)
    with caplog.at_level(logging.WARNING):
        agent.apply_snapshot(snapshot)
    assert not any("ps_contacts" in sql or '"cdr"' in sql for sql, _ in conn.writes())
    assert "protected table ps_contacts" in caplog.text and "protected table cdr" in caplog.text


def test_wrong_event_is_rejected(tmp_path):
    agent, conn, _, _ = make_agent(tmp_path)
    with pytest.raises(agent_mod.AgentError, match="expected 'demo'"):
        agent.apply_snapshot({**sample_snapshot(), "event": "other"})
    assert conn.writes() == []


def test_unknown_table_is_skipped_after_one_schema_refresh(tmp_path, caplog):
    snapshot = sample_snapshot(ps_future={"key": "id", "rows": [{"id": "a", "foo": "bar"}]})
    agent, conn, client, _ = make_agent(tmp_path)
    with caplog.at_level(logging.WARNING):
        stats = agent.apply_snapshot(snapshot)
    client.fetch_schema.assert_called_once()  # tried to learn the table once
    assert "ps_future" not in stats
    assert not any("ps_future" in sql for sql, _ in conn.executed)
    assert "unknown table ps_future in snapshot - skipped" in caplog.text
    assert conn.writes()[-1][0] == "COMMIT"  # the rest was still applied


def test_unknown_column_dropped_after_one_schema_refresh(tmp_path, caplog):
    snapshot = sample_snapshot()
    snapshot["tables"]["ps_aors"]["rows"][0]["qualify_frequency"] = 60
    agent, conn, client, _ = make_agent(tmp_path)
    with caplog.at_level(logging.WARNING):
        agent.apply_snapshot(snapshot)
    client.fetch_schema.assert_called_once()
    assert conn.ddl_applied == 1
    # information_schema re-read after the DDL: startup-less agent fetches once + once after refresh
    assert len(conn.statements("SELECT table_name")) == 2
    aor_inserts = [(sql, rows) for sql, rows in conn.writes() if '"ps_aors"' in sql and sql.startswith("INSERT")]
    assert aor_inserts == [('INSERT INTO "ps_aors" ("id", "max_contacts", "mailboxes") VALUES (%s, %s, %s)',
                            [("demo-aaaa", 1, "4242@pet-demo")])]
    assert "dropping column(s) qualify_frequency from ps_aors" in caplog.text


def test_new_column_is_used_when_schema_refresh_adds_it(tmp_path):
    snapshot = sample_snapshot()
    snapshot["tables"]["ps_aors"]["rows"][0]["qualify_frequency"] = 60

    def add_column(conn):
        conn.columns["ps_aors"].append("qualify_frequency")

    agent, conn, client, _ = make_agent(tmp_path, conn=FakeConn(on_ddl=add_column))
    agent.apply_snapshot(snapshot)
    client.fetch_schema.assert_called_once()
    aor_sql = [sql for sql, _ in conn.writes() if sql.startswith('INSERT INTO "ps_aors"')]
    assert aor_sql == ['INSERT INTO "ps_aors" ("id", "max_contacts", "mailboxes", "qualify_frequency") '
                       'VALUES (%s, %s, %s, %s)']


def test_plan_tables_rejects_weird_identifiers():
    plans, missing, unknown = plan_tables(
        {"ps_endpoints": {"rows": [{"id": "a", 'x"; drop table': 1}]}, 'evil"; --': {"rows": []}},
        {"ps_endpoints": ["id"]},
    )
    assert unknown == ['evil"; --']
    assert missing == {"ps_endpoints": ['x"; drop table']}
    assert plans[0].insert_sql() == 'INSERT INTO "ps_endpoints" ("id") VALUES (%s)'


# ----------------------------------------------------------------------------- cycle / state
def test_cycle_applies_reloads_saves_state_and_heartbeats(tmp_path, caplog):
    agent, conn, client, reloader = make_agent(tmp_path, snapshot=sample_snapshot("v1"))
    with caplog.at_level(logging.INFO, logger="pet.venue_agent"):
        changed = agent.cycle()
    assert changed is True
    client.fetch_snapshot.assert_called_once_with("")  # nothing applied yet -> no If-None-Match
    reloader.reload.assert_called_once()
    assert agent.state.version == "v1"
    assert agent.state.load_snapshot()["version"] == "v1"
    assert agent.poll_interval == 20  # taken from the snapshot (POLL_INTERVAL unset, heartbeat sent none)
    hb = client.heartbeat.call_args.args[0]
    assert hb["version"] == "v1" and hb["event"] == "demo"
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1 and "applied v1" in infos[0].message and "heartbeat ok" in infos[0].message


def test_cycle_304_keeps_database_untouched_but_heartbeats(tmp_path, caplog):
    agent, conn, client, reloader = make_agent(tmp_path)
    agent.state.save(sample_snapshot("v1"))
    client.fetch_snapshot.side_effect = NotModified()
    with caplog.at_level(logging.DEBUG, logger="pet.venue_agent"):
        changed = agent.cycle()
    assert changed is False
    client.fetch_snapshot.assert_called_once_with("v1")
    assert conn.writes() == []
    reloader.reload.assert_not_called()
    client.heartbeat.assert_called_once()
    assert client.heartbeat.call_args.args[0]["version"] == "v1"
    assert not [r for r in caplog.records if r.levelno == logging.INFO]  # unchanged -> DEBUG only


def test_heartbeat_payload_includes_local_contacts(tmp_path):
    contacts = [("demo-aaaa;@abc", "sip:demo-aaaa@10.0.0.5:5060", "demo-aaaa", 1757840000)]
    agent, conn, client, reloader = make_agent(tmp_path, conn=FakeConn(contacts=contacts))
    agent.state.save(sample_snapshot("v9"))
    agent.last_error = "asterisk reload: boom"
    reloader.probe.return_value = False
    payload = agent.heartbeat_payload()
    assert payload == {
        "event": "demo", "version": "v9", "hostname": "venue-box", "agent_version": "1.0.0",
        "asterisk_ok": False, "message": "asterisk reload: boom",
        "contacts": [{"id": "demo-aaaa;@abc", "uri": "sip:demo-aaaa@10.0.0.5:5060", "endpoint": "demo-aaaa",
                      "expiration_time": 1757840000}],
    }
    json.dumps(payload)  # must be serialisable as sent


def test_heartbeat_response_updates_poll_interval_and_behind(tmp_path):
    agent, _, client, _ = make_agent(tmp_path)
    client.heartbeat.return_value = {"ok": True, "current_version": "v2", "poll_interval": 42, "behind": True}
    agent.heartbeat()
    assert agent.poll_interval == 42 and agent.behind is True
    client.heartbeat.side_effect = PetHTTPError(503, "u")
    assert agent.heartbeat() is None  # never raises


def test_startup_reapplies_cached_snapshot_when_db_is_empty(tmp_path):
    agent, conn, client, reloader = make_agent(tmp_path)
    agent.state.save(sample_snapshot("cached"))
    client.fetch_schema.side_effect = PetHTTPError(None, "u", "offline")  # PET unreachable
    agent.startup()
    assert [sql for sql, _ in conn.writes() if sql.startswith("DELETE")]  # re-applied
    reloader.reload.assert_called_once()


def test_startup_leaves_populated_db_alone(tmp_path):
    agent, conn, client, reloader = make_agent(tmp_path, conn=FakeConn(counts={"ps_endpoints": 3}))
    agent.state.save(sample_snapshot("cached"))
    agent.startup()
    assert conn.writes() == []
    reloader.reload.assert_not_called()


def test_run_once_exit_codes_and_error_heartbeat(tmp_path):
    agent, conn, client, _ = make_agent(tmp_path, snapshot=sample_snapshot("v1"))
    assert agent.run(once=True) == 0
    agent, conn, client, _ = make_agent(tmp_path)
    client.fetch_snapshot.side_effect = PetHTTPError(500, "u", "boom")
    assert agent.run(once=True) == 1
    # PET was reachable (a status code came back) -> the error is reported through the heartbeat
    assert client.heartbeat.call_args.args[0]["message"].startswith("HTTP 500")
    agent, conn, client, _ = make_agent(tmp_path)
    client.fetch_snapshot.side_effect = PetHTTPError(None, "u", "refused")
    assert agent.run(once=True) == 1
    client.heartbeat.assert_not_called()  # unreachable: pointless


def test_backoff_progression():
    backoff = Backoff(15)
    assert [backoff.next_delay() for _ in range(7)] == [15, 30, 60, 120, 240, 300, 300]
    backoff.reset()
    assert backoff.next_delay() == 15
    assert Backoff(0).next_delay() == 1  # never a busy loop


def test_run_loop_backs_off_and_stops_on_sigterm(tmp_path):
    agent, conn, client, _ = make_agent(tmp_path)
    client.fetch_snapshot.side_effect = PetHTTPError(None, "u", "refused")
    delays = []

    def fake_wait(delay):
        delays.append(delay)
        if len(delays) == 3:
            agent.stop.set()  # what the SIGTERM handler does
        return agent.stop.is_set()

    agent.stop.wait = fake_wait
    assert agent.run() == 0
    assert delays == [15, 30, 60]


# ----------------------------------------------------------------------------- AMI reload
class FakeSocket:
    """Answers every action with ``Response: Success`` (or the scripted response for a Module)."""

    def __init__(self, fail_modules=()):
        self.sent: list[str] = []
        self.buffer = b"Asterisk Call Manager/7.0.3\r\n"
        self.fail_modules = set(fail_modules)
        self.closed = False

    def sendall(self, data: bytes):
        packet = data.decode()
        self.sent.append(packet)
        headers = dict(line.split(": ", 1) for line in packet.strip().split("\r\n"))
        ok = headers.get("Module") not in self.fail_modules
        # an unsolicited event first - the client must skip it
        self.buffer += b"Event: FullyBooted\r\nPrivilege: system,all\r\n\r\n"
        self.buffer += (f"Response: {'Success' if ok else 'Error'}\r\nActionID: {headers['ActionID']}\r\n"
                        f"Message: {'ok' if ok else 'Module not found'}\r\n\r\n").encode()

    def recv(self, n):
        chunk, self.buffer = self.buffer[:n], self.buffer[n:]
        return chunk

    def close(self):
        self.closed = True


def _ami_factory(sock: FakeSocket):
    def factory(host, port, user, password, timeout):
        return AMI(host, port, user, password, timeout, connect=lambda addr, timeout: sock)
    return factory


def test_ami_reload_sends_login_reload_per_module_and_logoff(tmp_path):
    sock = FakeSocket()
    cfg = make_config(tmp_path, ami_host="asterisk", ami_user="pet", ami_password="pw", reload_cmd="echo no")
    reloader = Reloader(cfg, ami_factory=_ami_factory(sock))
    assert reloader.mode == "ami"  # AMI wins over ASTERISK_RELOAD
    assert reloader.reload() == "AMI reload of res_pjsip.so, pbx_config.so, app_voicemail.so"
    actions = [re.search(r"Action: (\w+)", p).group(1) for p in sock.sent]
    assert actions == ["Login", "Reload", "Reload", "Reload", "Logoff"]
    assert "Username: pet\r\nSecret: pw\r\n" in sock.sent[0]
    modules = [re.search(r"Module: (\S+)", p).group(1) for p in sock.sent if "Module:" in p]
    assert modules == ["res_pjsip.so", "pbx_config.so", "app_voicemail.so"]
    assert all(p.endswith("\r\n\r\n") for p in sock.sent)
    assert sock.closed


def test_ami_reload_failure_and_probe(tmp_path):
    cfg = make_config(tmp_path, ami_host="asterisk", ami_user="pet", ami_password="pw")
    reloader = Reloader(cfg, ami_factory=_ami_factory(FakeSocket(fail_modules={"app_voicemail.so"})))
    with pytest.raises(ReloadError, match="app_voicemail.so"):
        reloader.reload()
    sock = FakeSocket()
    assert Reloader(cfg, ami_factory=_ami_factory(sock)).probe() is True
    assert "Action: Ping" in sock.sent[1]

    def refuse(addr, timeout):
        raise OSError("connection refused")

    def factory(host, port, user, password, timeout):
        return AMI(host, port, user, password, timeout, connect=refuse)

    assert Reloader(cfg, ami_factory=factory).probe() is False
    with pytest.raises(ReloadError, match="connection refused"):
        Reloader(cfg, ami_factory=factory).reload()


def test_cli_reload_runs_command_and_disabled_mode(tmp_path):
    cfg = make_config(tmp_path, reload_cmd='asterisk -rx "dialplan reload"')
    run = mock.MagicMock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
    assert Reloader(cfg, run=run).reload() == "reload command ok"
    run.assert_called_once_with('asterisk -rx "dialplan reload"', shell=True, capture_output=True, text=True,
                                timeout=120)
    run.return_value = mock.Mock(returncode=1, stdout="", stderr="Unable to connect to remote asterisk")
    with pytest.raises(ReloadError, match="Unable to connect"):
        Reloader(cfg, run=run).reload()
    disabled = Reloader(make_config(tmp_path, reload_cmd="  "), run=run, which=lambda name: None)
    assert disabled.mode == "disabled" and disabled.reload() == "reload disabled" and disabled.probe() is None


# ----------------------------------------------------------------------------- --check
def test_check_reports_ok(tmp_path):
    agent, conn, client, reloader = make_agent(tmp_path, conn=FakeConn(counts={"ps_endpoints": 2}),
                                               snapshot=sample_snapshot("v7"))
    out = io.StringIO()
    assert agent.check(out=out) == 0
    text = out.getvalue()
    assert "[OK  ] PET schema" in text and "[OK  ] PET snapshot: version v7" in text
    assert "[OK  ] database: connected (8 table(s)" in text
    assert "[OK  ] realtime tables: ps_endpoints=2" in text
    assert "[OK  ] asterisk: reload mode cli, probe ok" in text
    assert "FAIL" not in text


def test_check_reports_failures(tmp_path):
    conn = FakeConn(columns={"ps_endpoints": ["id"]})
    agent, conn, client, reloader = make_agent(tmp_path, conn=conn)
    client.fetch_schema.side_effect = PetHTTPError(401, "https://pet.example.org/...", "bad secret")
    client.fetch_snapshot.side_effect = PetHTTPError(None, "https://pet.example.org/...", "refused")
    reloader.probe.return_value = None
    out = io.StringIO()
    assert agent.check(out=out) == 1
    text = out.getvalue()
    assert "[FAIL] PET schema: HTTP 401" in text and "[FAIL] PET snapshot: connection failed" in text
    assert "[FAIL] realtime tables: ps_endpoints=0; MISSING: ps_auths" in text
    assert "[--  ] asterisk: reload mode cli, probe not possible" in text


def test_main_check_exit_code_and_config_error(tmp_path, monkeypatch):
    monkeypatch.delenv("PET_URL", raising=False)
    assert agent_mod.main(["--check"]) == 2  # missing config
    monkeypatch.setenv("PET_URL", "https://pet.example.org")
    monkeypatch.setenv("PET_EVENT", "demo")
    monkeypatch.setenv("PET_PBX_HOOK_SECRET", "x")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    fake_agent = mock.MagicMock()
    fake_agent.check.return_value = 0
    with mock.patch.object(agent_mod, "Agent", return_value=fake_agent):
        assert agent_mod.main(["--check"]) == 0
    fake_agent.check.assert_called_once()
    fake_agent.run.assert_not_called()
