import pytest
from django.urls import reverse

from apps.conferences import services
from apps.conferences.models import ConferenceParticipant
from apps.extensions.models import Extension
from apps.pbx import get_pbx
from apps.pbx.base import ChannelState

pytestmark = pytest.mark.django_db


@pytest.fixture
def pbx():
    p = get_pbx()
    p.reset()
    return p


def test_create_room_registers_conference_extension(event, user, member):
    room = services.create_room(event, user, "4500", "1234", name="Orga", max_participants=10)
    ext = Extension.objects.get(number="4500", event=event)
    assert ext.type == "conference" and ext.state == "active"
    assert ext.config["pin"] == "1234" and ext.config["max"] == 10 and room.owner == user
    services.update_room(room, actor=user, pin="", max_participants=5)
    ext.refresh_from_db()
    assert ext.config["pin"] == "" and ext.config["max"] == 5 and room.is_open


def test_pin_validation_and_flag(event, user, member, settings):
    with pytest.raises(services.ConferenceError):
        services.create_room(event, user, "4500", "12")
    settings.DIAL_FEATURES = {**settings.DIAL_FEATURES, "conferences": False}
    with pytest.raises(services.ConferenceError):
        services.create_room(event, user, "4500", "")


def test_refresh_participants_and_kick(event, user, member, pbx):
    room = services.create_room(event, user, "4500")
    pbx.channels = [
        ChannelState(id="c1", caller="4242", callee="4500", state="up", extra={"event": "demo"}),
        ChannelState(id="c2", caller="4300", callee="", state="up",
                     extra={"event": "demo", "bridge": "dial-demo-4500"}),
        ChannelState(id="c3", caller="4301", callee="4700", state="up", extra={"event": "demo"}),
    ]
    ps = services.refresh_participants(room)
    assert sorted(p.caller_number for p in ps) == ["4242", "4300"]
    services.kick(ps[0], actor=user)
    assert pbx.hungup == ["c1"]
    ps = services.refresh_participants(room)
    assert [p.caller_number for p in ps] == ["4300"]
    assert ConferenceParticipant.objects.filter(room=room, left_at__isnull=False).count() == 1


def test_pbx_route_reads_pin(client, event, user, member, settings):
    services.create_room(event, user, "4500", "4321")
    settings.DIAL_PBX_HOOK_SECRET = "s3cret"
    d = client.get("/api/v1/pbx/route/?event=demo&number=4500", HTTP_X_DIAL_PBX_SECRET="s3cret").json()
    assert d["conference"] == {"name": "dial-demo-4500", "pin": "4321"}


def test_views(client, event, user, other_user, orga, member, pbx):
    client.force_login(user)
    assert client.get(reverse("conferences:index", args=[event.slug])).status_code == 200
    r = client.post(reverse("conferences:new", args=[event.slug]),
                    {"number": "4500", "name": "Team", "pin": "1234", "max_participants": 10, "is_public": "on"})
    assert r.status_code == 302, r.content.decode()[:500]
    room = Extension.objects.get(number="4500").conference_room
    pbx.channels = [ChannelState(id="c1", caller="4242", callee="4500", state="up", extra={"event": "demo"})]
    r = client.get(reverse("conferences:detail", args=[event.slug, room.pk]))
    assert r.status_code == 200 and "Kick" in r.content.decode() and "1234" in r.content.decode()
    p = room.active_participants.get()

    client.force_login(other_user)  # public room: visible but no kick / no pin
    r = client.get(reverse("conferences:detail", args=[event.slug, room.pk]))
    assert r.status_code == 200 and "Kick" not in r.content.decode()
    assert client.post(reverse("conferences:kick", args=[event.slug, room.pk, p.pk])).status_code == 403

    client.force_login(orga)
    assert client.post(reverse("conferences:kick", args=[event.slug, room.pk, p.pk])).status_code == 302
    assert pbx.hungup == ["c1"]


def test_api(client, event, user, member, pbx):
    client.force_login(user)
    r = client.post("/api/v1/conferences/rooms/", {"event": "demo", "number": "4500", "pin": "1234"})
    assert r.status_code == 201 and "pin" not in r.json()
    pk = r.json()["id"]
    assert client.get(f"/api/v1/conferences/rooms/{pk}/participants/").json() == []
    r = client.patch(f"/api/v1/conferences/rooms/{pk}/", {"max_participants": 7}, content_type="application/json")
    assert r.status_code == 200 and Extension.objects.get(number="4500").config["max"] == 7
