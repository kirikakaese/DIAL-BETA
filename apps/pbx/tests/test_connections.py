"""Per-event venue PBX/DECT connections: adapter resolution, caching, hook secrets, health, orga page."""
import pytest
from django.test import Client
from rest_framework.test import APIClient

from apps.dect import get_dect, reset_dect_cache
from apps.dect.backends.dummy import DummyDECT
from apps.dect.models import DECTConnection
from apps.pbx import get_pbx, pbx_connection, reset_pbx_cache
from apps.pbx.backends.dummy import DummyPBX
from apps.pbx.forms import DECTConnectionForm, PBXConnectionForm
from apps.pbx.models import PBXConnection

pytestmark = pytest.mark.django_db

SERVER_SECRET = "server-secret"
EVENT_SECRET = "venue-secret"


@pytest.fixture(autouse=True)
def _defaults(settings):
    settings.PET_PBX_BACKEND = "apps.pbx.backends.dummy.DummyPBX"
    settings.PET_DECT_BACKEND = "apps.dect.backends.dummy.DummyDECT"
    settings.PET_PBX_HOOK_SECRET = SERVER_SECRET
    reset_pbx_cache()
    reset_dect_cache()
    yield
    reset_pbx_cache()
    reset_dect_cache()


@pytest.fixture
def pbx_conn(event):
    return PBXConnection.objects.create(event=event, backend="dummy", ari_url="http://10.1.1.5:8088/ari",
                                        ari_user="pet", ari_password="pw", hook_secret=EVENT_SECRET)


@pytest.fixture
def dect_conn(event):
    return DECTConnection.objects.create(event=event, backend="dummy", host="10.1.1.9", user="omm", password="pw")


# --------------------------------------------------------------------------- resolution / caching

def test_no_connection_falls_back_to_default(event):
    assert get_pbx(event) is get_pbx()
    assert get_dect(event) is get_dect()
    assert pbx_connection(event) is None


def test_event_connection_yields_distinct_configured_adapter(event, pbx_conn, dect_conn):
    pbx = get_pbx(event)
    assert isinstance(pbx, DummyPBX) and pbx is not get_pbx()
    assert pbx.config["ARI_URL"] == "http://10.1.1.5:8088/ari"
    assert pbx.config["SIP_DOMAIN"] == event.sip_domain
    dect = get_dect(event)
    assert isinstance(dect, DummyDECT) and dect is not get_dect()
    assert dect.config["HOST"] == "10.1.1.9" and dect.config["PORT"] == 12622


def test_adapter_is_cached_until_connection_changes(event, pbx_conn):
    first = get_pbx(event)
    assert get_pbx(event) is first
    pbx_conn.ari_url = "http://10.1.1.6:8088/ari"
    pbx_conn.save()  # bumps updated_at -> new adapter
    second = get_pbx(event)
    assert second is not first and second.config["ARI_URL"].endswith("1.6:8088/ari")
    reset_pbx_cache()
    assert get_pbx(event) is not second


def test_deleting_connection_returns_to_default(event, pbx_conn):
    assert get_pbx(event) is not get_pbx()
    pbx_conn.delete()
    reset_pbx_cache()
    assert get_pbx(event) is get_pbx()


def test_unknown_backend_key_raises(event):
    conn = PBXConnection(event=event, backend="nope")
    with pytest.raises(ValueError):
        _ = conn.backend_path


def test_config_inherits_server_defaults_for_blank_fields(event, settings):
    settings.ASTERISK = {**settings.ASTERISK, "ARI_USER": "srv-user", "ARI_PASSWORD": "srv-pw"}
    conn = PBXConnection.objects.create(event=event, backend="asterisk", ari_url="http://x:8088/ari")
    cfg = conn.config()
    assert cfg["ARI_USER"] == "srv-user" and cfg["ARI_PASSWORD"] == "srv-pw" and cfg["AMI_HOST"] == ""


# --------------------------------------------------------------------------- hook secret per event

def test_hook_accepts_event_secret_and_rejects_server_secret(event, pbx_conn):
    c = APIClient()
    ok = c.post("/api/v1/pbx/hooks/extension-idle/", {"event": "demo", "number": "4242"},
                HTTP_X_PET_PBX_SECRET=EVENT_SECRET)
    assert ok.status_code == 200
    bad = c.post("/api/v1/pbx/hooks/extension-idle/", {"event": "demo", "number": "4242"},
                 HTTP_X_PET_PBX_SECRET=SERVER_SECRET)
    assert bad.status_code == 401


def test_hook_without_connection_uses_server_secret(event):
    c = APIClient()
    assert c.post("/api/v1/pbx/hooks/extension-idle/", {"event": "demo", "number": "4242"},
                  HTTP_X_PET_PBX_SECRET=SERVER_SECRET).status_code == 200
    assert c.post("/api/v1/pbx/hooks/extension-idle/", {"event": "demo", "number": "4242"},
                  HTTP_X_PET_PBX_SECRET=EVENT_SECRET).status_code == 401


def test_blank_event_hook_secret_falls_back_to_server(event, pbx_conn):
    pbx_conn.hook_secret = ""
    pbx_conn.save()
    c = APIClient()
    assert c.post("/api/v1/pbx/hooks/extension-idle/", {"event": "demo", "number": "4242"},
                  HTTP_X_PET_PBX_SECRET=SERVER_SECRET).status_code == 200


def test_unknown_event_with_valid_secret_is_400_not_401(event):
    c = APIClient()
    r = c.post("/api/v1/pbx/hooks/feature-code/", {"event": "nope"}, HTTP_X_PET_PBX_SECRET=SERVER_SECRET)
    assert r.status_code == 400
    r = c.post("/api/v1/pbx/hooks/feature-code/", {"event": "nope"}, HTTP_X_PET_PBX_SECRET="wrong")
    assert r.status_code == 401


# --------------------------------------------------------------------------- health

def test_health_per_event(event, pbx_conn):
    c = APIClient()
    r = c.get("/api/v1/health/")
    assert r.status_code == 200 and r.json()["event"] is None
    r = c.get("/api/v1/health/", {"event": "demo"})
    assert r.status_code == 200 and r.json()["event"] == "demo" and r.json()["pbx"]["ok"] is True
    assert c.get("/api/v1/health/", {"event": "nope"}).status_code == 404


# --------------------------------------------------------------------------- orga page

URL = "/e/demo/pbx/"


def _pbx_post(action="save-pbx", **over):
    data = {"action": action, "pbx-backend": "dummy", "pbx-ari_url": "http://10.1.1.5:8088/ari",
            "pbx-ari_user": "pet", "pbx-ari_password": "secret1", "pbx-ari_app": "pet", "pbx-ami_port": "5038",
            "pbx-hook_secret": EVENT_SECRET}
    data.update(over)
    return data


def _dect_post(**over):
    data = {"action": "save-dect", "dect-backend": "dummy", "dect-host": "10.1.1.9", "dect-port": "12622",
            "dect-user": "omm", "dect-password": "pw1", "dect-verify_tls": ""}
    data.update(over)
    return data


def test_page_forbidden_for_plain_member(client: Client, event, member, user):
    client.force_login(user)
    assert client.get(URL).status_code == 403


def test_page_for_orga_shows_defaults(client: Client, event, orga):
    client.force_login(orga)
    r = client.get(URL)
    assert r.status_code == 200
    body = r.content.decode()
    assert "server default" in body and "X-PET-PBX-Secret" in body
    assert "/api/v1/pbx/route/" in body


def test_save_pbx_and_dect(client: Client, event, orga):
    client.force_login(orga)
    r = client.post(URL, _pbx_post())
    assert r.status_code == 302
    conn = PBXConnection.objects.get(event=event)
    assert conn.backend == "dummy" and conn.ari_password == "secret1" and conn.hook_secret == EVENT_SECRET
    r = client.post(URL, _dect_post())
    assert r.status_code == 302
    d = DECTConnection.objects.get(event=event)
    assert d.host == "10.1.1.9" and d.password == "pw1" and d.verify_tls is False
    assert get_pbx(event).config["ARI_URL"] == "http://10.1.1.5:8088/ari"


def test_empty_secret_keeps_stored_value(client: Client, event, orga, pbx_conn, dect_conn):
    client.force_login(orga)
    client.post(URL, _pbx_post(**{"pbx-ari_password": "", "pbx-hook_secret": "", "pbx-ari_user": "changed"}))
    pbx_conn.refresh_from_db()
    assert pbx_conn.ari_user == "changed" and pbx_conn.ari_password == "pw" and pbx_conn.hook_secret == EVENT_SECRET
    client.post(URL, _dect_post(**{"dect-password": "", "dect-user": "admin"}))
    dect_conn.refresh_from_db()
    assert dect_conn.user == "admin" and dect_conn.password == "pw"


def test_asterisk_requires_ari_url_and_omm_requires_host(client: Client, event, orga):
    client.force_login(orga)
    r = client.post(URL, _pbx_post(**{"pbx-backend": "asterisk", "pbx-ari_url": ""}))
    assert r.status_code == 200 and not PBXConnection.objects.filter(event=event).exists()
    assert "ARI URL is required" in r.content.decode()
    r = client.post(URL, _dect_post(**{"dect-backend": "omm", "dect-host": ""}))
    assert r.status_code == 200 and not DECTConnection.objects.filter(event=event).exists()


def test_reset_deletes_connection(client: Client, event, orga, pbx_conn, dect_conn):
    client.force_login(orga)
    assert client.post(URL, {"action": "reset-pbx"}).status_code == 302
    assert not PBXConnection.objects.filter(event=event).exists()
    assert client.post(URL, {"action": "reset-dect"}).status_code == 302
    assert not DECTConnection.objects.filter(event=event).exists()
    assert get_pbx(event) is get_pbx()


def test_test_action_renders_health(client: Client, event, orga, pbx_conn, dect_conn):
    client.force_login(orga)
    r = client.post(URL, {"action": "test"})
    assert r.status_code == 200
    assert "reachable" in r.content.decode()


def test_secret_help_text_only_when_stored(event, pbx_conn):
    fresh = PBXConnectionForm(instance=PBXConnection(event=event), prefix="pbx")
    assert not fresh.fields["ari_password"].help_text
    bound = PBXConnectionForm(instance=pbx_conn, prefix="pbx")
    assert "Leave empty" in str(bound.fields["ari_password"].help_text)
    assert not bound.fields["ami_password"].help_text  # nothing stored there
    d = DECTConnectionForm(instance=DECTConnection(event=event), prefix="dect")
    assert not d.fields["password"].help_text


def test_sidebar_links_to_infrastructure(client: Client, event, orga):
    client.force_login(orga)
    r = client.get("/e/demo/")
    assert r.status_code == 200 and URL in r.content.decode()


def test_orga_dashboard_shows_venue_status(client: Client, event, orga, pbx_conn):
    client.force_login(orga)
    body = client.get("/e/demo/orga/").content.decode()
    assert 'id="venue-connection"' in body and URL in body
    assert "10.1.1.5:8088/ari" in body and "server default" in body  # PBX configured, DECT not


# --------------------------------------------------------------------------- REST API

API = "/api/v1/pbx/connection/"


def test_api_connection_requires_orga(event, user, orga, member):
    c = APIClient()
    assert c.get(API, {"event": "demo"}).status_code in (401, 403)
    c.force_authenticate(user)
    assert c.get(API, {"event": "demo"}).status_code == 403
    c.force_authenticate(orga)
    assert c.get(API, {"event": "nope"}).status_code == 404
    r = c.get(API, {"event": "demo"})
    assert r.status_code == 200
    d = r.json()
    assert d["pbx"] is None and d["dect"] is None
    assert d["server_default"] == {"pbx": True, "dect": True}
    assert d["effective"] == {"pbx": "dummy", "dect": "dummy"}
    assert set(d["backends"]["pbx"]) >= {"asterisk", "dummy"}


def test_api_connection_set_show_reset(event, orga):
    c = APIClient()
    c.force_authenticate(orga)
    r = c.patch(API + "?event=demo", {
        "pbx": {"backend": "dummy", "ari_url": "http://10.1.1.5:8088/ari", "ari_password": "s3cret",
                "hook_secret": EVENT_SECRET},
        "dect": {"backend": "dummy", "host": "10.1.1.9", "password": "pw"},
    }, format="json")
    assert r.status_code == 200, r.content
    d = r.json()
    assert d["pbx"]["ari_url"] == "http://10.1.1.5:8088/ari" and d["pbx"]["has_ari_password"] is True
    assert "ari_password" not in d["pbx"] and "hook_secret" not in d["pbx"]
    assert d["dect"]["host"] == "10.1.1.9" and d["dect"]["has_password"] is True
    assert d["server_default"] == {"pbx": False, "dect": False}
    assert get_pbx(event).config["ARI_URL"] == "http://10.1.1.5:8088/ari"

    # partial update keeps everything else, including secrets
    r = c.patch(API + "?event=demo", {"pbx": {"ari_user": "venue"}}, format="json")
    assert r.status_code == 200
    conn = PBXConnection.objects.get(event=event)
    assert conn.ari_user == "venue" and conn.ari_password == "s3cret" and conn.hook_secret == EVENT_SECRET
    assert conn.ari_url == "http://10.1.1.5:8088/ari"

    # the per-event hook secret set via API is live
    assert APIClient().post("/api/v1/pbx/hooks/extension-idle/", {"event": "demo", "number": "4242"},
                            HTTP_X_PET_PBX_SECRET=EVENT_SECRET).status_code == 200

    r = c.delete(API + "?event=demo&part=pbx")
    assert r.status_code == 200 and r.json()["pbx"] is None and r.json()["dect"] is not None
    r = c.delete(API + "?event=demo")
    assert r.status_code == 200 and r.json()["dect"] is None
    assert get_pbx(event) is get_pbx()


def test_api_connection_validation(event, orga):
    c = APIClient()
    c.force_authenticate(orga)
    r = c.put(API + "?event=demo", {"pbx": {"backend": "asterisk"}}, format="json")
    assert r.status_code == 400 and "ari_url" in r.json()["errors"]["pbx"]
    assert not PBXConnection.objects.filter(event=event).exists()
    assert c.put(API + "?event=demo", {"nothing": 1}, format="json").status_code == 400
    assert c.delete(API + "?event=demo&part=bogus").status_code == 400
