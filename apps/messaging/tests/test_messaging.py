import pytest
from django.urls import reverse

from apps.events.models import EventMembership
from apps.extensions.services import register
from apps.messaging import services
from apps.messaging.models import Message

pytestmark = pytest.mark.django_db


def test_send_to_extension_via_dummy_dect(event, user, other_user, ext_alice, handset, dect):
    msg = services.send_to_extension(event, other_user, ext_alice, "Hello alice")
    assert msg.state == "sent" and msg.sent_at is not None
    assert dect.messages[-1] == {"ppn": "1", "text": "Hello alice", "priority": "normal"}


def test_send_without_handset_fails(event, user, other_user, ext_alice, dect):
    msg = services.send_to_extension(event, other_user, ext_alice, "Hello")
    assert msg.state == "failed" and "no DECT handset" in msg.error and dect.messages == []


def test_flag_off_returns_none(event, user, other_user, ext_alice, handset, settings):
    settings.DIAL_FEATURES = {**settings.DIAL_FEATURES, "messaging": False}
    assert services.send_to_extension(event, other_user, ext_alice, "x") is None
    assert services.broadcast(event, other_user, "x") is None
    assert services.send_to_group(event, other_user, None, "x") == []


def test_broadcast_all_and_group(event, user, other_user, orga, angels, ext_alice, handset, dect):
    register(event, other_user, "4300", "dect")  # no handset -> failed
    bc = services.broadcast(event, orga, "Doors open")
    assert (bc.sent_count, bc.failed_count) == (1, 1)
    assert dect.messages[-1]["priority"] == "high"

    m = EventMembership.objects.create(event=event, user=user, role="user")
    m.groups.add(angels)
    bc = services.broadcast(event, orga, "Angels: shift change", group=angels)
    assert (bc.sent_count, bc.failed_count) == (1, 0) and bc.target == "group"
    assert Message.objects.filter(recipient_group=angels, state="sent").count() == 1


def test_validation(event, other_user, ext_alice, handset):
    with pytest.raises(services.MessagingError):
        services.send_to_extension(event, other_user, ext_alice, "")
    with pytest.raises(services.MessagingError):
        services.send_to_extension(event, other_user, ext_alice, "x" * 481)


def test_handle_inbound(event, user, ext_alice, handset):
    msg = services.handle_inbound(event, "1", "help at stage")
    assert msg.direction == "in" and msg.sender_extension == ext_alice and msg.sender == user


def test_views(client, event, user, other_user, orga, ext_alice, handset, member):
    client.force_login(user)
    r = client.get(reverse("messaging:index", args=[event.slug]))
    assert r.status_code == 200 and "Send a message" in r.content.decode()
    r = client.post(reverse("messaging:index", args=[event.slug]), {"number": "4242", "text": "yo"})
    assert r.status_code == 302 and Message.objects.filter(state="sent", text="yo").exists()
    assert client.get(reverse("messaging:broadcast", args=[event.slug])).status_code == 403

    client.force_login(orga)
    r = client.post(reverse("messaging:broadcast", args=[event.slug]), {"target": "all", "text": "hi all"})
    assert r.status_code == 302 and Message.objects.filter(text="hi all", state="sent").count() == 1


def test_api(client, event, user, orga, ext_alice, handset, member):
    client.force_login(user)
    r = client.post("/api/v1/messaging/messages/", {"event": "demo", "recipient_number": "4242", "text": "api"})
    assert r.status_code == 201 and r.json()["state"] == "sent"
    listing = client.get("/api/v1/messaging/messages/").json()
    rows = listing["results"] if isinstance(listing, dict) else listing
    assert len(rows) == 1 and rows[0]["recipient"] == "4242"
    assert client.post("/api/v1/messaging/broadcast/", {"event": "demo", "text": "x"}).status_code == 403
    client.force_login(orga)
    r = client.post("/api/v1/messaging/broadcast/", {"event": "demo", "text": "x"})
    assert r.status_code == 201 and r.json()["sent_count"] == 1
