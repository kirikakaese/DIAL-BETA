"""GSM: per-event endpoint enablement, dial string via the event trunk, SIM registration hook + orga page."""
import json

import pytest
from django.urls import reverse

from apps.devices import endpoint_types, services
from apps.devices.models import Device, DeviceBinding
from apps.extensions import services as ext_services

pytestmark = pytest.mark.django_db

HOOK = "/prov/gsm/register/"


@pytest.fixture
def gsm_event(event):
    event.has_gsm = True
    event.save(update_fields=["has_gsm"])
    return event


@pytest.fixture
def hdr(settings):
    settings.DIAL_PBX_HOOK_SECRET = "hook-secret"
    return {"HTTP_X_DIAL_PBX_SECRET": "hook-secret"}


def test_enabled_for_gsm_depends_on_event_flag(event):
    gsm = endpoint_types.get("gsm")
    assert gsm.enabled is False
    assert gsm.enabled_for(event) is False
    assert "gsm" not in [t.key for t in endpoint_types.all_types_for(event)]
    event.has_gsm = True
    assert gsm.enabled_for(event) is True
    assert "gsm" in [t.key for t in endpoint_types.all_types_for(event)]
    assert gsm.enabled_for(None) is False
    # other types keep following the global flag
    assert endpoint_types.get("dect").enabled_for(event) is True
    assert endpoint_types.get("webrtc").enabled_for(event) is False
    assert "gsm" not in [t.key for t in endpoint_types.all_types()]  # legacy API unchanged


def test_gsm_dial_string_uses_event_trunk(gsm_event, user):
    d = Device.objects.create(event=gsm_event, owner=user, type="gsm", msisdn="4915112345")
    assert endpoint_types.get("gsm").dial(d) == "PJSIP/4915112345@gsm-gateway"
    gsm_event.gsm_trunk = "osmo"
    assert endpoint_types.get("gsm").dial(d) == "PJSIP/4915112345@osmo"
    gsm_event.gsm_trunk = ""
    assert endpoint_types.get("gsm").dial(d) == "PJSIP/4915112345@gsm-gateway"


def test_issue_gsm_register_token_and_defaults(gsm_event, user):
    d = Device.objects.create(event=gsm_event, owner=user, type="gsm")
    assert (d.gsm_2g, d.gsm_3g, d.gsm_4g, d.gsm_5g) == (True, False, False, False)
    assert d.gsm_generations == ["2G"]
    token = d.issue_gsm_register_token()
    d.refresh_from_db()
    assert len(token) == 6 and token.isdigit() and d.gsm_register_token == token


def test_gsm_register_service(gsm_event, user, member):
    ext = ext_services.register(gsm_event, user, "4711", "gsm")
    d = Device.objects.create(event=gsm_event, owner=user, type="gsm")
    DeviceBinding.objects.create(extension=ext, device=d)
    token = d.issue_gsm_register_token()
    with pytest.raises(services.GSMRegisterError):
        services.gsm_register(gsm_event, "000000", "262420000000001")
    with pytest.raises(services.GSMRegisterError):
        services.gsm_register(gsm_event, token, "")
    dev = services.gsm_register(gsm_event, token, "262420000000001", "4915112345")
    assert dev == d and dev.imsi == "262420000000001" and dev.msisdn == "4915112345"
    assert dev.gsm_registered_at and dev.state == "subscribed" and dev.gsm_register_token == ""
    with pytest.raises(services.GSMRegisterError):  # single use
        services.gsm_register(gsm_event, token, "262420000000001")


def test_gsm_register_hook_requires_secret(client, gsm_event, user, hdr):
    d = Device.objects.create(event=gsm_event, owner=user, type="gsm")
    token = d.issue_gsm_register_token()
    payload = {"event": "demo", "token": token, "imsi": "262420000000001", "msisdn": "1234"}
    r = client.post(HOOK, payload)
    assert r.status_code == 401 and r.json()["registered"] is False
    r = client.post(HOOK, payload, HTTP_X_DIAL_PBX_SECRET="wrong")
    assert r.status_code == 401
    d.refresh_from_db()
    assert d.imsi == "" and d.gsm_register_token == token

    r = client.post(HOOK, json.dumps(payload), content_type="application/json", **hdr)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["registered"] is True and body["device"] == str(d.pk) and body["msisdn"] == "1234"
    d.refresh_from_db()
    assert d.imsi == "262420000000001" and d.gsm_registered_at is not None

    # unknown token -> 404 JSON; unknown event -> 400; GET -> 405
    r = client.post(HOOK, {"event": "demo", "token": "000000", "imsi": "1"}, **hdr)
    assert r.status_code == 404 and r.json()["registered"] is False
    assert client.post(HOOK, {"event": "nope", "token": token, "imsi": "1"}, **hdr).status_code == 400
    assert client.get(HOOK, **hdr).status_code == 405


def test_gsm_register_hook_falls_back_to_ari_password(client, gsm_event, user, settings):
    if hasattr(settings, "DIAL_PBX_HOOK_SECRET"):
        del settings.DIAL_PBX_HOOK_SECRET
    d = Device.objects.create(event=gsm_event, owner=user, type="gsm")
    token = d.issue_gsm_register_token()
    r = client.post(HOOK, {"event": "demo", "token": token, "imsi": "262420000000001"},
                    HTTP_X_DIAL_PBX_SECRET=settings.ASTERISK["ARI_PASSWORD"])
    assert r.status_code == 200


def test_gsm_register_hook_refuses_event_without_gsm(client, event, user, hdr):
    d = Device.objects.create(event=event, owner=user, type="gsm")
    token = d.issue_gsm_register_token()
    r = client.post(HOOK, {"event": "demo", "token": token, "imsi": "1"}, **hdr)
    assert r.status_code == 400 and "not enabled" in r.json()["detail"]


def test_gsm_orga_page(client, event, user, orga, member):
    url = reverse("devices:gsm", args=[event.slug])
    client.force_login(user)
    assert client.get(url).status_code == 403
    client.force_login(orga)
    r = client.get(url)
    assert r.status_code == 200 and b"not enabled" in r.content
    event.has_gsm = True
    event.save()
    ext = ext_services.register(event, user, "4711", "gsm")
    assert ext.is_active
    r = client.post(url, {"number": "4711", "name": "Bobs Nokia", "msisdn": "555", "imsi": ""})
    assert r.status_code == 302, r.content
    d = Device.objects.get(event=event, type="gsm")
    assert d.owner == user and d.msisdn == "555" and d.gsm_register_token
    assert DeviceBinding.objects.filter(extension=ext, device=d).exists()
    r = client.get(url)
    assert b"Bobs Nokia" in r.content and d.gsm_register_token.encode() in r.content
    r = client.post(reverse("devices:gsm_token", args=[event.slug, d.pk]))
    assert r.status_code == 302
    d.refresh_from_db()
    assert d.gsm_register_token
    # device page shows the SIM section
    client.force_login(user)
    r = client.get(reverse("portal:device_detail", args=[event.slug, d.pk]))
    assert r.status_code == 200 and b"GSM SIM" in r.content
