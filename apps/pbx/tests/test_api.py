"""Hook auth + dispatch, route lookup, resync/status."""
import sys
import types

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from apps.extensions.models import ExtensionType
from apps.pbx import get_pbx, reset_pbx_cache
from apps.pbx.models import DialplanEntry

from .conftest import bind, make_device, make_extension

pytestmark = pytest.mark.django_db

SECRET = "hook-secret-123"
HDR = {"HTTP_X_DIAL_PBX_SECRET": SECRET}


@pytest.fixture(autouse=True)
def _secret(settings):
    settings.DIAL_PBX_HOOK_SECRET = SECRET


@pytest.fixture
def client():
    return APIClient()


def fake_module(monkeypatch, name, **funcs):
    mod = types.ModuleType(name)
    for k, v in funcs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


# --------------------------------------------------------------------------- hooks

def test_hook_requires_secret(client, event):
    r = client.post("/api/v1/pbx/hooks/feature-code/", {"event": "demo"})
    assert r.status_code == 401
    r = client.post("/api/v1/pbx/hooks/feature-code/", {"event": "demo"}, HTTP_X_DIAL_PBX_SECRET="wrong")
    assert r.status_code == 401


def test_hook_falls_back_to_ari_password(client, event, settings):
    del settings.DIAL_PBX_HOOK_SECRET
    r = client.post("/api/v1/pbx/hooks/extension-idle/", {"event": "demo", "number": "4242"},
                    HTTP_X_DIAL_PBX_SECRET=settings.ASTERISK["ARI_PASSWORD"])
    assert r.status_code == 200


def test_hook_unknown_kind_and_event(client, event):
    assert client.post("/api/v1/pbx/hooks/bogus/", {"event": "demo"}, **HDR).status_code == 404
    r = client.post("/api/v1/pbx/hooks/feature-code/", {"event": "nope"}, **HDR)
    assert r.status_code == 400 and r.json()["handled"] is False


def test_hook_missing_service_degrades(client, event, monkeypatch):
    monkeypatch.delitem(sys.modules, "apps.callback.services", raising=False)
    monkeypatch.delitem(sys.modules, "apps.callgroups.services", raising=False)
    monkeypatch.setitem(sys.modules, "apps.callback.services", None)  # forces ImportError
    monkeypatch.setitem(sys.modules, "apps.callgroups.services", None)
    r = client.post("/api/v1/pbx/hooks/feature-code/",
                    {"event": "demo", "caller": "4242", "code": "*66", "target": "4243"}, **HDR)
    assert r.status_code == 200 and r.json() == {"handled": False}


def test_feature_code_dispatch_form_encoded(client, event, monkeypatch):
    calls = []
    fake_module(monkeypatch, "apps.callback.services",
                handle_feature_code=lambda ev, caller, code, target:
                    calls.append(("cb", ev.slug, caller, code, target)) or False)
    fake_module(monkeypatch, "apps.callgroups.services",
                handle_feature_code=lambda ev, caller, code, target:
                    calls.append(("cg", ev.slug, caller, code, target)) or True)
    r = client.post("/api/v1/pbx/hooks/feature-code/",
                    "event=demo&caller=4242&code=*71&target=4400", content_type="application/x-www-form-urlencoded",
                    **HDR)
    assert r.status_code == 200 and r.json()["handled"] is True
    assert calls == [("cb", "demo", "4242", "*71", "4400"), ("cg", "demo", "4242", "*71", "4400")]


def test_feature_code_first_handler_wins(client, event, monkeypatch):
    fake_module(monkeypatch, "apps.callback.services", handle_feature_code=lambda *a: True)
    cg = fake_module(monkeypatch, "apps.callgroups.services",
                     handle_feature_code=lambda *a: pytest.fail("should not be called"))
    r = client.post("/api/v1/pbx/hooks/feature-code/",
                    {"event": "demo", "caller": "4242", "code": "*66", "target": "4243"}, format="json", **HDR)
    assert r.json()["handled"] is True
    assert cg is not None


def test_extension_idle_and_cdr_and_voicemail_and_survey(client, event, monkeypatch):
    seen = {}
    fake_module(monkeypatch, "apps.callback.services",
                on_extension_idle=lambda ev, number: seen.setdefault("idle", number) or 2)
    fake_module(monkeypatch, "apps.stats.services", ingest_cdr=lambda ev, rec: seen.setdefault("cdr", rec) or True)
    fake_module(monkeypatch, "apps.voicemail.services",
                store_message=lambda ev, mb, caller, path, dur: seen.setdefault("vm", (mb, caller, path, dur)) or 1)

    class RFP:
        name = "RFP-Hackcenter-3"

    fake_module(monkeypatch, "apps.dect.services", log_site_survey=lambda ev, caller: RFP())

    r = client.post("/api/v1/pbx/hooks/extension-idle/", {"event": "demo", "number": "4242"}, **HDR)
    assert r.json()["handled"] is True and seen["idle"] == "4242"

    r = client.post("/api/v1/pbx/hooks/cdr/", {"event": "demo", "src": "4242", "dst": "4243", "duration": "12",
                                               "disposition": "ANSWERED", "junk": "x"}, **HDR)
    assert r.json()["handled"] is True
    assert seen["cdr"]["src"] == "4242" and "junk" not in seen["cdr"]

    r = client.post("/api/v1/pbx/hooks/voicemail/", {"event": "demo", "mailbox": "4242", "caller": "4243",
                                                     "file_path": "/var/spool/x/msg0000.wav", "duration": "7.4"},
                    **HDR)
    assert r.json()["handled"] is True and seen["vm"] == ("4242", "4243", "/var/spool/x/msg0000.wav", 7)

    r = client.post("/api/v1/pbx/hooks/site-survey/", {"event": "demo", "caller": "4242"}, **HDR)
    assert r.json() == {"handled": True, "rfp": "RFP-Hackcenter-3", "say": "RFP-Hackcenter-3"}


def test_hook_service_exception_is_swallowed(client, event, monkeypatch):
    def boom(*a):
        raise RuntimeError("db on fire")

    fake_module(monkeypatch, "apps.callback.services", on_extension_idle=boom)
    r = client.post("/api/v1/pbx/hooks/extension-idle/", {"event": "demo", "number": "1"}, **HDR)
    assert r.status_code == 200 and r.json()["handled"] is False


# --------------------------------------------------------------------------- route

def test_route_requires_secret_and_params(client, event):
    assert client.get("/api/v1/pbx/route/?event=demo&number=4242").status_code == 401
    assert client.get("/api/v1/pbx/route/?event=demo", **HDR).status_code == 400
    assert client.get("/api/v1/pbx/route/?event=nope&number=1", **HDR).status_code == 400


def test_route_endpoint_extension(client, event, user):
    ext = make_extension(event, "4242", owner=user, forward_noanswer="4243", ring_timeout=20, priority=3)
    bind(ext, make_device(event, "demo-aaaa"))
    bind(ext, make_device(event, "demo-bbbb"), priority=1)
    r = client.get("/api/v1/pbx/route/?event=demo&number=4242", **HDR)
    assert r.status_code == 200
    d = r.json()
    assert d["type"] == "sip" and d["context"] == "dial-demo"
    assert d["targets"] == ["PJSIP/demo-aaaa", "PJSIP/demo-bbbb"]
    assert d["dial_string"] == "PJSIP/demo-aaaa&PJSIP/demo-bbbb"
    assert d["strategy"] == "parallel" and d["timeout"] == 20 and d["priority"] == 3
    assert d["forward"] == {"busy": "", "noanswer": "4243", "unconditional": ""}
    assert d["forward_noanswer"] == "4243" and d["allow_callback"] is True


def test_route_group_uses_callgroups_service(client, event, user, monkeypatch):
    grp = make_extension(event, "4400", etype=ExtensionType.GROUP, owner=user)
    a = make_extension(event, "4242", owner=user)
    bind(a, make_device(event, "demo-aaaa"))
    make_extension(event, "4243", owner=user)  # member without device -> Local channel
    fake_module(monkeypatch, "apps.callgroups.services",
                dial_targets=lambda ext: {"targets": ["4242", "4243"], "strategy": "serial"})
    d = client.get("/api/v1/pbx/route/?event=demo&number=4400", **HDR).json()
    assert d["type"] == "group" and d["strategy"] == "serial"
    assert d["targets"] == ["PJSIP/demo-aaaa", "Local/4243@dial-demo"]
    assert d["dial_string"] == "PJSIP/demo-aaaa&Local/4243@dial-demo"
    assert grp.number == "4400"


def test_route_group_without_service_uses_config(client, event, user, monkeypatch):
    monkeypatch.setitem(sys.modules, "apps.callgroups.services", None)
    make_extension(event, "4400", etype=ExtensionType.GROUP, owner=user, config={"members": ["4242"]})
    d = client.get("/api/v1/pbx/route/?event=demo&number=4400", **HDR).json()
    assert d["targets"] == ["Local/4242@dial-demo"]


def test_route_ivr_and_announcement(client, event, user, monkeypatch):
    monkeypatch.setitem(sys.modules, "apps.ivr.services", None)
    make_extension(event, "4800", etype=ExtensionType.IVR, owner=user,
                   config={"audio": "dial/menu", "options": {"1": "4242", "2": "4243"}, "timeout": 7})
    make_extension(event, "4801", etype=ExtensionType.ANNOUNCEMENT, owner=user, config={"audio": "dial/hello"})
    d = client.get("/api/v1/pbx/route/?event=demo&number=4800", **HDR).json()
    assert d["type"] == "ivr" and d["ivr_greeting"] == "dial/menu" and d["ivr_timeout"] == 7
    assert d["ivr_options"] == {"1": "4242", "2": "4243"}
    d = client.get("/api/v1/pbx/route/?event=demo&number=4801", **HDR).json()
    assert d["type"] == "announcement" and d["ivr_greeting"] == "dial/hello" and d["ivr_options"] == {}


def test_route_unknown_number_asks_emergency_and_federation(client, event, monkeypatch):
    fake_module(monkeypatch, "apps.emergency.services", route=lambda ev, n: "PJSIP/4242" if n == "112" else None)
    fake_module(monkeypatch, "apps.federation.services",
                route=lambda ev, n: "PJSIP/1234@camp2" if n.startswith("8") else None)
    d = client.get("/api/v1/pbx/route/?event=demo&number=112", **HDR).json()
    assert d["type"] == "emergency" and d["dial_string"] == "PJSIP/4242" and d["priority"] == 100
    d = client.get("/api/v1/pbx/route/?event=demo&number=81234", **HDR).json()
    assert d["type"] == "federation" and d["targets"] == ["PJSIP/1234@camp2"]
    d = client.get("/api/v1/pbx/route/?event=demo&number=7777", **HDR).json()
    assert d["type"] is None and d["targets"] == []


def test_route_conference(client, event, user):
    make_extension(event, "4500", etype=ExtensionType.CONFERENCE, owner=user, config={"pin": "42"})
    d = client.get("/api/v1/pbx/route/?event=demo&number=4500", **HDR).json()
    assert d["conference"] == {"name": "dial-demo-4500", "pin": "42"}


# --------------------------------------------------------------------------- dialplan export

def test_dialplan_export(client, event, user):
    make_extension(event, "4242", owner=user)
    assert client.get("/api/v1/pbx/dialplan/?shell=1").status_code == 401
    r = client.get("/api/v1/pbx/dialplan/?shell=1", **HDR)
    assert r.status_code == 200 and r["Content-Type"].startswith("text/plain")
    assert r.content.decode() == "[dial-demo]\ninclude => dial-internal\nswitch => Realtime/@\n"
    r = client.get("/api/v1/pbx/dialplan/?event=demo", **HDR)
    assert "exten => 4242,1,Set(__DIAL_EVENT=demo)" in r.content.decode()


# --------------------------------------------------------------------------- orga tools

@override_settings(DIAL_PBX_BACKEND="apps.pbx.backends.asterisk.AsteriskPBX")
def test_resync_requires_orga(client, event, user, orga):
    reset_pbx_cache()
    try:
        get_pbx().use_ami = False
        assert client.post("/api/v1/pbx/resync/?event=demo").status_code in (401, 403)
        client.force_authenticate(user)
        assert client.post("/api/v1/pbx/resync/?event=demo").status_code == 403
        client.force_authenticate(orga)
        make_extension(event, "4242", owner=user)
        r = client.post("/api/v1/pbx/resync/?event=demo")
        assert r.status_code == 200
        assert r.json() == {"event": "demo", "synced": 1, "backend": "asterisk"}
        assert DialplanEntry.objects.filter(context="dial-demo", exten="4242").exists()
        assert client.post("/api/v1/pbx/resync/?event=nope").status_code == 404
    finally:
        reset_pbx_cache()


def test_resync_with_dummy_backend(client, event, admin):
    reset_pbx_cache()
    client.force_authenticate(admin)
    make_extension(event, "4242")
    r = client.post("/api/v1/pbx/resync/", {"event": "demo"})
    assert r.status_code == 200 and r.json()["synced"] == 1 and r.json()["backend"] == "dummy"
    assert "demo" in get_pbx().synced_events


def test_status(client, event, orga):
    reset_pbx_cache()
    pbx = get_pbx()
    pbx.channels = [{"id": "c1", "caller": "4242", "callee": "4243", "state": "up", "extra": {"event": "demo"}},
                    {"id": "c2", "caller": "1", "callee": "2", "state": "up", "extra": {"event": "other"}}]
    client.force_authenticate(orga)
    r = client.get("/api/v1/pbx/status/?event=demo")
    assert r.status_code == 200
    d = r.json()
    assert d["health"]["ok"] is True and d["backend"] == "dummy"
    assert [c["id"] for c in d["channels"]] == ["c1"]
    pbx.channels = []
