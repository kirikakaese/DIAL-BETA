"""Venue-agent config sync: snapshot endpoint, schema DDL, heartbeats, agent-mode adapter, portal, CLI."""
import datetime as dt
import json
from unittest import mock

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import ServiceAccount
from apps.api import cli
from apps.core.models import AuditLog
from apps.events.models import Event, Webhook
from apps.pbx import get_pbx, outbox, reset_pbx_cache
from apps.pbx import snapshot as snap
from apps.pbx.backends.asterisk import AsteriskPBX
from apps.pbx.base import PBXError
from apps.pbx.models import DialplanEntry, PBXConnection, PsAor, PsAuth, PsContact, PsEndpoint, VoicemailUser

from .conftest import bind, make_device, make_extension

pytestmark = pytest.mark.django_db

SERVER_SECRET = "server-secret"
EVENT_SECRET = "venue-secret"
SNAPSHOT = "/api/v1/pbx/snapshot/"
SCHEMA = "/api/v1/pbx/snapshot/schema/"
HEARTBEAT = "/api/v1/pbx/agent/heartbeat/"
ALL_TABLES = ("ps_endpoints", "ps_auths", "ps_aors", "ps_contacts", "ps_endpoint_id_ips", "extensions",
              "voicemail_users", "cdr")


@pytest.fixture(autouse=True)
def _defaults(settings):
    settings.PET_PBX_HOOK_SECRET = SERVER_SECRET
    cache.clear()
    reset_pbx_cache()
    yield
    reset_pbx_cache()
    cache.clear()


@pytest.fixture
def agent_conn(event):
    return PBXConnection.objects.create(event=event, backend="dummy", provisioning="agent", hook_secret=EVENT_SECRET,
                                        agent_poll_interval=10)


@pytest.fixture
def other_event(db):
    today = dt.date.today()
    return Event.objects.create(name="Other Camp", slug="other", state=Event.State.REGISTRATION,
                                start_date=today, end_date=today + dt.timedelta(days=2), sip_domain="other.pet.local")


@pytest.fixture
def populated(pbx, event, other_event, user):
    """Both events have one extension with a bound SIP device (endpoint/auth/aor + dialplan + mailbox rows)."""
    ext = make_extension(event, "4242", owner=user, display_name="Alice")
    bind(ext, make_device(event, "demo-aaaa", owner=user))
    pbx.sync_extension(ext)
    other = make_extension(other_event, "5555", owner=user, display_name="Zed")
    bind(other, make_device(other_event, "other-zzzz", owner=user))
    pbx.sync_extension(other)
    return ext


# --------------------------------------------------------------------------- service layer

def test_rows_are_scoped_to_the_event(event, other_event, populated):
    data = snap.build_snapshot(event)
    assert data["event"] == "demo" and set(data["tables"]) == set(snap.SNAPSHOT_TABLES)
    assert [r["id"] for r in data["tables"]["ps_endpoints"]["rows"]] == ["demo-aaaa"]
    assert [r["id"] for r in data["tables"]["ps_auths"]["rows"]] == ["demo-aaaa"]
    assert [r["id"] for r in data["tables"]["ps_aors"]["rows"]] == ["demo-aaaa"]
    assert {r["context"] for r in data["tables"]["extensions"]["rows"]} == {"pet-demo"}
    assert {r["exten"] for r in data["tables"]["extensions"]["rows"]} >= {"4242"}
    assert data["tables"]["voicemail_users"]["key"] == "uniqueid"
    assert [r["mailbox"] for r in data["tables"]["voicemail_users"]["rows"]] == ["4242"]
    # rows carry the DB column names and plain JSON values
    ep = data["tables"]["ps_endpoints"]["rows"][0]
    assert ep["accountcode"] == "demo" and ep["context"] == "pet-demo" and ep["device_state_busy_at"] is None
    assert isinstance(data["tables"]["voicemail_users"]["rows"][0]["stamp"], str)
    other = snap.build_snapshot(other_event)
    assert [r["id"] for r in other["tables"]["ps_endpoints"]["rows"]] == ["other-zzzz"]
    assert {r["context"] for r in other["tables"]["extensions"]["rows"]} == {"pet-other"}


def test_version_is_stable_and_changes_with_content(pbx, event, other_event, populated, user):
    v1 = snap.snapshot_version(event)
    assert v1 == snap.build_snapshot(event)["version"] == snap.snapshot_version(event) and len(v1) == 64
    # re-syncing the same extension re-creates dialplan rows (new ids) and bumps the mailbox ``stamp`` -
    # neither changes what Asterisk sees, so the version stays
    pbx.sync_extension(populated)
    assert snap.snapshot_version(event) == v1
    # the other event's rows do not influence this version
    other_v = snap.snapshot_version(other_event)
    pbx.sync_extension(make_extension(other_event, "5556", owner=user))
    assert snap.snapshot_version(event) == v1 and snap.snapshot_version(other_event) != other_v
    pbx.sync_extension(make_extension(event, "4243", owner=user))
    v2 = snap.snapshot_version(event)
    assert v2 != v1
    # row order does not matter
    tables = snap.build_tables(event)
    tables["extensions"]["rows"].reverse()
    assert snap.tables_version(tables) == v2


def test_schema_contains_every_realtime_table(capsys):
    sql = snap.venue_schema_sql()
    for table in ALL_TABLES:
        assert f'CREATE TABLE IF NOT EXISTS "{table}"' in sql, table
    assert '"end" timestamp with time zone' in sql  # reserved word stays quoted
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in sql and '("context", "exten", "priority")' in sql
    assert "varchar_pattern_ops" not in sql
    call_command("pbx_venue_schema")
    assert capsys.readouterr().out.strip() == sql.strip()


def test_schema_fallback_ddl_covers_all_tables(monkeypatch):
    monkeypatch.setattr(snap, "_pg_schema_editor", mock.Mock(side_effect=ImportError("no psycopg")))
    sql = snap.venue_schema_sql()
    for table in ALL_TABLES:
        assert f'CREATE TABLE IF NOT EXISTS "{table}"' in sql, table
    assert '"stamp" timestamp with time zone NULL' in sql and '"id" varchar(40) NOT NULL PRIMARY KEY' in sql


def test_stale_and_behind_properties(event, agent_conn, populated):
    assert agent_conn.agent_is_stale  # never seen
    agent_conn.agent_last_seen = timezone.now() - dt.timedelta(seconds=25)
    assert not agent_conn.agent_is_stale  # 3 x 10 s
    agent_conn.agent_last_seen = timezone.now() - dt.timedelta(seconds=31)
    assert agent_conn.agent_is_stale
    assert agent_conn.agent_behind
    agent_conn.agent_version = snap.snapshot_version(event)
    assert not agent_conn.agent_behind


def test_apply_heartbeat_without_connection_still_replaces_contacts(event, populated):
    rows = [{"id": "demo-aaaa;@abc", "endpoint": "demo-aaaa", "uri": "sip:1@10.0.0.9", "expiration_time": "99"}]
    assert snap.apply_heartbeat(event, {"version": "x", "contacts": rows}) is None
    c = PsContact.objects.get(id="demo-aaaa;@abc")
    assert c.endpoint == "demo-aaaa" and c.expiration_time == 99


# --------------------------------------------------------------------------- snapshot endpoint / auth

def test_snapshot_requires_secret_or_scoped_token(event, agent_conn, orga, user, member, populated):
    c = APIClient()
    assert c.get(SNAPSHOT, {"event": "demo"}).status_code == 401
    assert c.get(SNAPSHOT, {"event": "demo"}, HTTP_X_PET_PBX_SECRET="wrong").status_code == 401
    assert c.get(SNAPSHOT, {"event": "demo"}, HTTP_X_PET_PBX_SECRET=SERVER_SECRET).status_code == 401
    r = c.get(SNAPSHOT, {"event": "demo"}, HTTP_X_PET_PBX_SECRET=EVENT_SECRET)
    assert r.status_code == 200 and r.json()["event"] == "demo" and r.json()["poll_interval"] == 10
    assert r["ETag"] == f'"{r.json()["version"]}"'
    assert c.get(SNAPSHOT, {"event": "nope"}, HTTP_X_PET_PBX_SECRET=SERVER_SECRET).status_code == 404
    assert c.get(SNAPSHOT, {"event": "nope"}, HTTP_X_PET_PBX_SECRET="wrong").status_code == 401

    _, scoped = ServiceAccount.issue(name="agent", owner=orga, event=event, scopes=["pbx:sync"])
    _, unscoped = ServiceAccount.issue(name="badge", owner=orga, event=event, scopes=["extensions:read"])
    _, not_orga = ServiceAccount.issue(name="plain", owner=user, event=event, scopes=["pbx:sync"])
    assert c.get(SNAPSHOT, {"event": "demo"}, HTTP_AUTHORIZATION=f"Bearer {scoped}").status_code == 200
    assert c.get(SNAPSHOT, {"event": "demo"}, HTTP_AUTHORIZATION=f"Bearer {unscoped}").status_code == 401
    assert c.get(SNAPSHOT, {"event": "demo"}, HTTP_AUTHORIZATION=f"Bearer {not_orga}").status_code == 401
    assert c.get(SCHEMA, {"event": "demo"}, HTTP_AUTHORIZATION=f"Bearer {scoped}").status_code == 200
    # an orga session works too (browser / smoke test)
    c.force_authenticate(orga)
    assert c.get(SNAPSHOT, {"event": "demo"}).status_code == 200
    c.force_authenticate(user)
    assert c.get(SNAPSHOT, {"event": "demo"}).status_code == 401


def test_snapshot_etag_304_and_change(pbx, event, agent_conn, populated, user):
    c = APIClient()
    r = c.get(SNAPSHOT, {"event": "demo"}, HTTP_X_PET_PBX_SECRET=EVENT_SECRET)
    etag = r["ETag"]
    r2 = c.get(SNAPSHOT, {"event": "demo"}, HTTP_X_PET_PBX_SECRET=EVENT_SECRET, HTTP_IF_NONE_MATCH=etag)
    assert r2.status_code == 304 and not r2.content and r2["ETag"] == etag
    weak = c.get(SNAPSHOT, {"event": "demo"}, HTTP_X_PET_PBX_SECRET=EVENT_SECRET, HTTP_IF_NONE_MATCH=f"W/{etag}")
    assert weak.status_code == 304
    pbx.sync_extension(make_extension(event, "4243", owner=user))
    r3 = c.get(SNAPSHOT, {"event": "demo"}, HTTP_X_PET_PBX_SECRET=EVENT_SECRET, HTTP_IF_NONE_MATCH=etag)
    assert r3.status_code == 200 and r3["ETag"] != etag
    assert {row["exten"] for row in r3.json()["tables"]["extensions"]["rows"]} >= {"4242", "4243"}


def test_schema_endpoint(event, agent_conn):
    r = APIClient().get(SCHEMA, {"event": "demo"}, HTTP_X_PET_PBX_SECRET=EVENT_SECRET)
    assert r.status_code == 200
    d = r.json()
    assert d["dialect"] == "postgresql" and set(d["tables"]) == set(ALL_TABLES)
    for table in ALL_TABLES:
        assert f'CREATE TABLE IF NOT EXISTS "{table}"' in d["sql"]
    assert APIClient().get(SCHEMA, {"event": "demo"}, HTTP_X_PET_PBX_SECRET="wrong").status_code == 401


# --------------------------------------------------------------------------- heartbeat

def test_heartbeat_updates_fields_and_replaces_contacts(event, other_event, agent_conn, populated):
    PsContact.objects.create(id="old;@1", endpoint="demo-aaaa", uri="sip:old")
    PsContact.objects.create(id="other;@1", endpoint="other-zzzz", uri="sip:other")
    before = PBXConnection.objects.get(pk=agent_conn.pk).updated_at
    c = APIClient()
    body = {"event": "demo", "version": "abc", "hostname": "venue-pbx", "agent_version": "1.0", "asterisk_ok": True,
            "message": "all good",
            "contacts": [
                {"id": "demo-aaaa;@new", "endpoint": "demo-aaaa", "uri": "sip:demo-aaaa@10.0.0.7:5060",
                 "expiration_time": 1_900_000_000, "user_agent": "Test/1", "via_port": 5060, "bogus": "x"},
                {"id": "sneaky;@1", "endpoint": "other-zzzz", "uri": "sip:evil"},  # not this event's endpoint
            ]}
    assert c.post(HEARTBEAT, body, format="json", HTTP_X_PET_PBX_SECRET="wrong").status_code == 401
    r = c.post(HEARTBEAT, body, format="json", HTTP_X_PET_PBX_SECRET=EVENT_SECRET)
    assert r.status_code == 200, r.content
    d = r.json()
    assert d["ok"] is True and d["poll_interval"] == 10 and d["behind"] is True and d["connection"] is True
    assert d["current_version"] == snap.snapshot_version(event)
    agent_conn.refresh_from_db()
    assert agent_conn.agent_version == "abc" and agent_conn.agent_host == "venue-pbx"
    assert agent_conn.agent_software == "1.0" and agent_conn.agent_asterisk_ok is True
    assert agent_conn.agent_message == "all good" and agent_conn.agent_last_seen is not None
    assert not agent_conn.agent_is_stale and agent_conn.agent_behind
    assert agent_conn.updated_at == before  # heartbeats do not invalidate the cached adapter
    assert not PsContact.objects.filter(id="old;@1").exists()
    assert PsContact.objects.filter(id="other;@1", endpoint="other-zzzz").exists()  # other event untouched
    assert not PsContact.objects.filter(id="sneaky;@1").exists()
    new = PsContact.objects.get(id="demo-aaaa;@new")
    assert new.expiration_time == 1_900_000_000 and new.via_port == 5060 and new.user_agent == "Test/1"

    # up to date once the agent reports the current version; contacts omitted -> kept
    r = c.post(HEARTBEAT, {"event": "demo", "version": d["current_version"], "asterisk_ok": False,
                           "message": "asterisk down"}, format="json", HTTP_X_PET_PBX_SECRET=EVENT_SECRET)
    assert r.json()["behind"] is False
    agent_conn.refresh_from_db()
    assert agent_conn.agent_asterisk_ok is False and not agent_conn.agent_behind
    assert PsContact.objects.filter(id="demo-aaaa;@new").exists()
    assert not AuditLog.objects.filter(event=event).exists()  # heartbeats are not audited


def test_heartbeat_with_service_token(event, agent_conn, orga):
    _, raw = ServiceAccount.issue(name="agent", owner=orga, event=event, scopes=["pbx:sync"])
    r = APIClient().post(HEARTBEAT, {"event": "demo", "version": ""}, format="json",
                         HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code == 200 and r.json()["ok"] is True


# --------------------------------------------------------------------------- agent-mode adapter

def _agent_pbx(event, **over):
    cfg = {"PROVISIONING": "agent", "ARI_URL": "", "AMI_HOST": "", "EVENT_ID": event.pk}
    cfg.update(over)
    return AsteriskPBX(config=cfg)


def test_agent_mode_reload_is_a_noop_and_never_raises(event, monkeypatch, settings):
    settings.PET_PBX_USE_AMI = True
    p = _agent_pbx(event)
    assert p.agent_mode and not p.use_ami
    p._reload_dialplan()  # no AMI host -> nothing to do
    p = _agent_pbx(event, AMI_HOST="10.255.255.1")
    assert p.use_ami

    def boom():
        raise PBXError("AMI connect failed")

    monkeypatch.setattr(p, "_ami", boom)
    p._reload_dialplan()

    def crash():
        raise OSError("network unreachable")

    monkeypatch.setattr(p, "_ami", crash)
    p._reload_dialplan()  # non-PBXError transport failure is logged in agent mode


def test_agent_mode_sync_event_writes_rows_without_pbx(event, user):
    ext = make_extension(event, "4242", owner=user, display_name="Alice")
    bind(ext, make_device(event, "demo-aaaa", owner=user))
    p = _agent_pbx(event)
    assert p.sync_event(event) == 1
    assert PsEndpoint.objects.filter(id="demo-aaaa").exists() and PsAuth.objects.filter(id="demo-aaaa").exists()
    assert PsAor.objects.filter(id="demo-aaaa").exists()
    assert DialplanEntry.objects.filter(context="pet-demo", exten="4242").exists()
    assert VoicemailUser.objects.filter(context="pet-demo", mailbox="4242").exists()


def test_agent_mode_status_and_health_from_heartbeats(event, agent_conn, user):
    ext = make_extension(event, "4242", owner=user)
    bind(ext, make_device(event, "demo-aaaa", owner=user))
    p = _agent_pbx(event)
    st = p.extension_status(ext)
    assert st.state == "unavailable" and st.registered_devices == 0
    PsContact.objects.create(id="demo-aaaa;@1", endpoint="demo-aaaa",
                             expiration_time=int(timezone.now().timestamp()) + 60)
    st = p.extension_status(ext)
    assert st.state == "idle" and st.registered_devices == 1
    h = p.health()
    assert h["ok"] is False and h["mode"] == "agent" and "no heartbeat" in h["error"]
    snap.apply_heartbeat(event, {"version": "", "asterisk_ok": True, "hostname": "venue-pbx"})
    h = p.health()
    assert h["ok"] is True and h["agent_host"] == "venue-pbx" and h["stale"] is False
    with pytest.raises(PBXError):
        p.active_channels(event)
    p.set_mwi(ext, 1)  # no ARI -> logged no-op
    with pytest.raises(PBXError):
        p.originate(event=event, destination="4242", caller_id="PET <9000>")


def test_config_in_agent_mode_does_not_fall_back_to_server_ari(event, agent_conn, settings):
    settings.ASTERISK = {**settings.ASTERISK, "ARI_URL": "http://server-asterisk:8088/ari"}
    assert agent_conn.config()["ARI_URL"] == "" and agent_conn.config()["PROVISIONING"] == "agent"
    agent_conn.provisioning = "shared_db"
    assert agent_conn.config()["ARI_URL"] == "http://server-asterisk:8088/ari"


# --------------------------------------------------------------------------- webhook

def test_snapshot_changed_webhook_from_outbox(event, agent_conn, user, settings):
    settings.PET_PBX_OUTBOX_SYNC = True
    Webhook.objects.create(event=event, name="nudge", url="http://agent.example/hook",
                           event_types=["pbx.snapshot.changed"])
    ext = make_extension(event, "4242", owner=user)
    with mock.patch("apps.events.webhooks.deliver.delay") as delay:
        outbox.deliver(outbox.enqueue("sync_extension", target=ext))
        assert delay.call_count == 1
        assert delay.call_args.args[1] == "pbx.snapshot.changed" and delay.call_args.args[2]["event"] == "demo"
        # nothing changed (dummy adapter writes no rows) -> no second notification
        outbox.deliver(outbox.enqueue("sync_extension", target=ext))
        assert delay.call_count == 1
        # a real row change moves the version -> notified again
        AsteriskPBX().sync_extension(ext)
        outbox.deliver(outbox.enqueue("sync_extension", target=ext))
        assert delay.call_count == 2
    agent_conn.provisioning = "shared_db"
    agent_conn.save()
    reset_pbx_cache()
    with mock.patch("apps.events.webhooks.deliver.delay") as delay:
        outbox.deliver(outbox.enqueue("sync_extension", target=ext))
        assert delay.call_count == 0  # shared-db events do not emit


# --------------------------------------------------------------------------- portal / API payload

def test_portal_page_renders_agent_card(client: Client, event, orga, agent_conn, populated):
    client.force_login(orga)
    body = client.get("/e/demo/pbx/").content.decode()
    assert 'id="venue-agent"' in body and "Venue agent" in body
    assert "PET_EVENT=demo" in body and f"PET_PBX_HOOK_SECRET={EVENT_SECRET}" in body
    assert "PET_URL=" in body and SNAPSHOT in body and HEARTBEAT in body
    assert "stale" in body and "never" in body  # no heartbeat yet
    snap.apply_heartbeat(event, {"version": snap.snapshot_version(event), "hostname": "venue-pbx",
                                 "agent_version": "1.2", "asterisk_ok": True})
    body = client.get("/e/demo/pbx/").content.decode()
    assert "venue-pbx" in body and "up to date" in body and 'class="badge badge-ok">online' in body
    # shared-db connections show no agent card
    agent_conn.provisioning = "shared_db"
    agent_conn.save()
    assert 'id="venue-agent"' not in client.get("/e/demo/pbx/").content.decode()


def test_portal_form_saves_agent_mode_without_ari_url(client: Client, event, orga):
    client.force_login(orga)
    r = client.post("/e/demo/pbx/", {"action": "save-pbx", "pbx-backend": "asterisk", "pbx-provisioning": "agent",
                                     "pbx-agent_poll_interval": "20", "pbx-ari_app": "pet", "pbx-ami_port": "5038",
                                     "pbx-hook_secret": EVENT_SECRET})
    assert r.status_code == 302, r.content.decode()[:500]
    conn = PBXConnection.objects.get(event=event)
    assert conn.provisioning == "agent" and conn.agent_poll_interval == 20 and conn.ari_url == ""
    assert isinstance(get_pbx(event), AsteriskPBX) and get_pbx(event).agent_mode
    r = client.post("/e/demo/pbx/", {"action": "save-pbx", "pbx-backend": "asterisk", "pbx-provisioning": "shared_db",
                                     "pbx-ari_app": "pet", "pbx-ami_port": "5038"})
    assert r.status_code == 200 and "ARI URL is required" in r.content.decode()


def test_orga_dashboard_shows_agent_line(client: Client, event, orga, agent_conn):
    client.force_login(orga)
    body = client.get("/e/demo/orga/").content.decode()
    assert "Venue agent" in body and "stale" in body
    snap.apply_heartbeat(event, {"version": snap.snapshot_version(event), "hostname": "venue-pbx"})
    body = client.get("/e/demo/orga/").content.decode()
    assert "venue-pbx" in body and 'class="badge badge-ok">ok' in body


def test_api_connection_payload_has_agent_fields(event, orga, agent_conn, populated):
    c = APIClient()
    c.force_authenticate(orga)
    d = c.get("/api/v1/pbx/connection/", {"event": "demo"}).json()["pbx"]
    assert d["provisioning"] == "agent" and d["agent_poll_interval"] == 10
    assert d["agent_is_stale"] is True and d["agent_behind"] is True and d["agent_last_seen"] is None
    assert d["snapshot_version"] == snap.snapshot_version(event)
    for k in ("agent_version", "agent_host", "agent_software", "agent_asterisk_ok", "agent_message"):
        assert k in d
    # agent_* are read-only, provisioning/poll interval are writable
    body = {"pbx": {"provisioning": "shared_db", "agent_poll_interval": 30, "agent_host": "hacker"}}
    r = c.patch("/api/v1/pbx/connection/?event=demo", body, format="json")
    assert r.status_code == 200, r.content
    agent_conn.refresh_from_db()
    assert agent_conn.agent_host == "" and agent_conn.provisioning == "shared_db"
    assert agent_conn.agent_poll_interval == 30


def test_api_connection_switch_mode_is_audited(event, orga, agent_conn):
    c = APIClient()
    c.force_authenticate(orga)
    r = c.patch("/api/v1/pbx/connection/?event=demo", {"pbx": {"provisioning": "shared_db", "agent_poll_interval": 30,
                                                               "ari_url": "http://10.1.1.5:8088/ari"}}, format="json")
    assert r.status_code == 200, r.content
    agent_conn.refresh_from_db()
    assert agent_conn.provisioning == "shared_db" and agent_conn.agent_poll_interval == 30
    assert AuditLog.objects.filter(event=event, message__contains="dummy/shared_db").exists()


# --------------------------------------------------------------------------- CLI

class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.content = json.dumps(payload).encode()
        self.text = self.content.decode()

    def json(self):
        return self._payload


def test_cli_connection_set_provisioning(capsys):
    state = {"event": "demo", "pbx": {"backend": "asterisk", "backend_label": "Asterisk", "provisioning": "agent",
                                       "ari_url": ""}, "dect": None, "effective": {"pbx": "asterisk", "dect": "dummy"}}
    with mock.patch("requests.Session.request", return_value=FakeResponse(state)) as req:
        rc = cli.main(["--url", "http://pet.test", "pbx", "connection", "set", "--event", "demo",
                       "--provisioning", "agent", "--agent-poll-interval", "20"])
    assert rc == 0
    assert req.call_args.args == ("PATCH", "http://pet.test/api/v1/pbx/connection/")
    assert req.call_args.kwargs["json"] == {"pbx": {"provisioning": "agent", "agent_poll_interval": 20}}
    assert "provisioning agent" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["pbx", "connection", "set", "--event", "demo", "--provisioning", "bogus"])


def test_cli_agent_status(capsys):
    state = {"event": "demo", "pbx": {"backend": "asterisk", "backend_label": "Asterisk", "provisioning": "agent",
                                       "agent_last_seen": "2026-09-14T10:00:00Z", "agent_host": "venue-pbx",
                                       "agent_software": "1.0", "agent_version": "aaa", "snapshot_version": "bbb",
                                       "agent_asterisk_ok": True, "agent_message": "", "agent_poll_interval": 15,
                                       "agent_is_stale": False, "agent_behind": True},
             "dect": None, "effective": {"pbx": "asterisk", "dect": "dummy"}}
    with mock.patch("requests.Session.request", return_value=FakeResponse(state)) as req:
        assert cli.main(["--url", "http://pet.test", "pbx", "agent", "status", "--event", "demo"]) == 0
    assert req.call_args.args == ("GET", "http://pet.test/api/v1/pbx/connection/")
    out = capsys.readouterr().out
    assert "venue agent behind" in out and "venue-pbx" in out and "aaa" in out and "bbb" in out
    with mock.patch("requests.Session.request", return_value=FakeResponse({**state, "pbx": None})):
        assert cli.main(["--url", "http://pet.test", "pbx", "agent", "status", "--event", "demo"]) == 0
    assert "server default" in capsys.readouterr().out


def test_cli_snapshot_dump(capsys, tmp_path):
    payload = {"event": "demo", "version": "abc123def456xyz", "generated_at": "2026-09-14T10:00:00Z",
               "poll_interval": 15, "tables": {"ps_endpoints": {"key": "id", "rows": [{"id": "demo-aaaa"}]},
                                               "extensions": {"key": "id", "rows": [{"id": 1}, {"id": 2}]}}}
    with mock.patch("requests.Session.request", return_value=FakeResponse(payload)) as req:
        assert cli.main(["--url", "http://pet.test", "pbx", "snapshot", "--event", "demo"]) == 0
    assert req.call_args.args == ("GET", "http://pet.test/api/v1/pbx/snapshot/")
    assert req.call_args.kwargs["params"] == {"event": "demo"}
    assert json.loads(capsys.readouterr().out) == payload
    out = tmp_path / "snap.json"
    with mock.patch("requests.Session.request", return_value=FakeResponse(payload)):
        assert cli.main(["--url", "http://pet.test", "pbx", "snapshot", "--event", "demo", "--out", str(out)]) == 0
    assert json.loads(out.read_text()) == payload
    assert "3 rows" in capsys.readouterr().out and "abc123def456" in out.read_text()
