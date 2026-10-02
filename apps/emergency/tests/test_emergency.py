import pytest
from django.urls import reverse

from apps.core.models import AuditLog
from apps.emergency import services
from apps.emergency.models import BroadcastAnnouncement, EmergencyIncident, EmergencyTarget
from apps.events.models import EventMembership
from apps.extensions.services import register
from apps.pbx import get_pbx

pytestmark = pytest.mark.django_db


@pytest.fixture
def pbx():
    p = get_pbx()
    p.reset()
    return p


@pytest.fixture
def security(event, orga):
    return register(event, orga, "4911", "dect", display_name="Security")


def test_route(event, orga, security):
    t = services.create_target(event, orga, "112", destination_extension=security, fallback_number="+49301234")
    assert services.route(event, "112") == "Local/4911@pet-demo"
    assert services.route(event, "110") is None
    security.state = "suspended"
    security.save()
    assert services.route(event, "112") == "+49301234"
    t.fallback_number = ""
    t.save()
    assert services.route(event, "112") is None


def test_create_target_validation(event, orga, security):
    with pytest.raises(services.EmergencyError):
        services.create_target(event, orga, "4242", destination_extension=security)
    with pytest.raises(services.EmergencyError):
        services.create_target(event, orga, "112")
    services.create_target(event, orga, "112", fallback_number="+49")
    services.create_target(event, orga, "112", destination_extension=security)  # update, not duplicate
    assert EmergencyTarget.objects.filter(event=event, number="112").count() == 1


def test_flag_off(event, orga, security, settings):
    services.create_target(event, orga, "112", destination_extension=security)
    settings.PET_FEATURES = {**settings.PET_FEATURES, "emergency": False}
    assert services.route(event, "112") is None
    assert services.broadcast_all(event, orga, "fire") is None
    assert services.log_incident(event, "112", "4242") is None


def test_log_and_resolve_incident(event, user, orga, member):
    ext = register(event, user, "4242", "dect")
    inc = services.log_incident(event, "112", "4242")
    assert inc.caller_extension == ext and inc.is_open
    services.resolve_incident(inc, orga, notes="false alarm")
    assert not inc.is_open and inc.handled_by == orga and "false alarm" in inc.notes


def test_broadcast_all_uses_pbx_and_messaging(event, user, other_user, orga, angels, member, pbx):
    register(event, user, "4242", "dect")
    register(event, other_user, "4300", "dect")
    ba = services.broadcast_all(event, orga, "Evacuate the main hall")
    assert ba.targets == 2 and len(pbx.originated) == 2
    assert {o["destination"] for o in pbx.originated} == {"4242", "4300"}
    assert pbx.originated[0]["caller_id"] == "EMERGENCY"
    assert pbx.originated[0]["variables"] == {"PET_ANNOUNCEMENT": "Evacuate the main hall", "PET_PRIORITY": "10"}
    assert ba.results["text_broadcast"] is not None  # messaging broadcast stored (no handsets -> failed, but logged)

    pbx.reset()
    EventMembership.objects.get(event=event, user=user).groups.add(angels)
    ba = services.broadcast_all(event, orga, "Angels to heaven", group=angels)
    assert ba.targets == 1 and pbx.originated[0]["destination"] == "4242"
    with pytest.raises(services.EmergencyError):
        services.broadcast_all(event, orga, "   ")


def test_set_priority_audits(event, user, orga, member):
    ext = register(event, user, "4242", "dect")
    services.set_priority(ext, 50, orga)
    ext.refresh_from_db()
    assert ext.priority == 50
    assert AuditLog.objects.filter(message="Priority changed").exists()
    assert list(services.preempt_candidates(event))[0] == ext
    services.set_priority(ext, 999, orga)
    assert ext.priority == 100


def test_pbx_route_api(client, event, orga, security, settings):
    services.create_target(event, orga, "112", destination_extension=security)
    settings.PET_PBX_HOOK_SECRET = "s3cret"
    d = client.get("/api/v1/pbx/route/?event=demo&number=112", HTTP_X_PET_PBX_SECRET="s3cret").json()
    assert d["type"] == "emergency" and d["dial_string"] == "Local/4911@pet-demo" and d["priority"] == 100
    r = client.post("/api/v1/emergency/incident-log/", {"event": "demo", "number": "112", "caller": "4911"},
                    HTTP_X_PET_PBX_SECRET="s3cret")
    assert r.status_code == 200 and r.json()["handled"] is True and EmergencyIncident.objects.count() == 1
    assert client.post("/api/v1/emergency/incident-log/", {"event": "demo", "number": "112"}).status_code == 401


def test_views(client, event, user, orga, member, security, pbx):
    url = reverse("emergency:index", args=[event.slug])
    client.force_login(user)
    assert client.get(url).status_code == 403
    client.force_login(orga)
    r = client.get(url)
    assert r.status_code == 200 and "START EMERGENCY BROADCAST" in r.content.decode()
    r = client.post(reverse("emergency:target_save", args=[event.slug]),
                    {"number": "112", "label": "Security", "destination_extension": str(security.pk),
                     "announce_location": "on", "priority": 100})
    assert r.status_code == 302 and services.route(event, "112") == "Local/4911@pet-demo"
    r = client.post(reverse("emergency:broadcast", args=[event.slug]), {"announcement": "Test", "confirm": "on"})
    assert r.status_code == 302 and BroadcastAnnouncement.objects.count() == 1 and len(pbx.originated) == 1
    assert client.post(reverse("emergency:broadcast", args=[event.slug]), {"announcement": "x"}).status_code == 302
    assert BroadcastAnnouncement.objects.count() == 1  # not confirmed -> rejected
    inc = services.log_incident(event, "112", "4911")
    r = client.post(reverse("emergency:incident_resolve", args=[event.slug, inc.pk]), {"notes": "ok"})
    assert r.status_code == 302
    inc.refresh_from_db()
    assert not inc.is_open
    r = client.post(reverse("emergency:priority", args=[event.slug, security.pk]), {"level": 42})
    assert r.status_code == 302
    security.refresh_from_db()
    assert security.priority == 42


def test_api(client, event, user, orga, member, security, pbx):
    client.force_login(user)
    assert client.get("/api/v1/emergency/targets/?event=demo").status_code == 403
    client.force_login(orga)
    r = client.post("/api/v1/emergency/targets/",
                    {"event": "demo", "number": "110", "destination_extension": str(security.pk)})
    assert r.status_code == 201 and r.json()["dial_target"] == "Local/4911@pet-demo"
    r = client.post("/api/v1/emergency/targets/", {"event": "demo", "number": "4242", "fallback_number": "1"})
    assert r.status_code == 400
    r = client.post("/api/v1/emergency/broadcast/", {"event": "demo", "announcement": "Test"})
    assert r.status_code == 201 and r.json()["targets"] == 1 and len(pbx.originated) == 1
    inc = services.log_incident(event, "112", "4911")
    r = client.post(f"/api/v1/emergency/incidents/{inc.pk}/resolve/?event=demo", {"notes": "done"})
    assert r.status_code == 200 and r.json()["resolved_at"] is not None
