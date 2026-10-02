"""Voicemail portal views and REST API."""
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework.test import APIClient

from apps.voicemail import services
from apps.voicemail.models import Mailbox, Message

pytestmark = [pytest.mark.django_db, pytest.mark.urls("apps.callback.tests.urls_stub")]


def _url(name, event, *args):
    return reverse(f"voicemail:{name}", args=[event.slug, *args])


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def test_index(client, event, user, member, ext_alice, ext_bob, wav_file, pbx):
    assert client.get(_url("index", event)).status_code == 302
    client.force_login(user)
    r = client.get(_url("index", event))
    assert r.status_code == 200 and Mailbox.objects.filter(extension=ext_alice).exists()
    assert "No messages" in r.content.decode()
    msg = services.store_message(event, "4242", "4300", str(wav_file), 5)
    services.store_message(event, "4300", "4242", "", 5)  # bob's - must not show
    r = client.get(_url("index", event))
    body = r.content.decode()
    assert "1 unread message" in body and "<audio controls" in body and _url("audio", event, msg.pk) in body
    assert "Bob Builder" in body and body.count("Mark read") == 1


def test_audio_mark_read_delete_permissions(client, event, user, other_user, orga, member, ext_alice, wav_file, pbx):
    msg = services.store_message(event, "4242", "4300", str(wav_file), 5)
    no_audio = services.store_message(event, "4242", "4300", "", 5)
    client.force_login(other_user)
    assert client.get(_url("audio", event, msg.pk)).status_code == 403
    assert client.post(_url("mark_read", event, msg.pk)).status_code == 403
    assert client.post(_url("delete", event, msg.pk)).status_code == 403
    client.force_login(user)
    r = client.get(_url("audio", event, msg.pk))
    assert r.status_code == 200 and r["Content-Type"] == "audio/wav"
    assert b"".join(r.streaming_content)[:4] == b"RIFF"
    assert client.get(_url("audio", event, no_audio.pk)).status_code == 404
    assert client.post(_url("mark_read", event, msg.pk)).status_code == 302
    msg.refresh_from_db()
    assert msg.is_read
    assert client.post(_url("mark_read", event, msg.pk), {"unread": "1"}).status_code == 302
    msg.refresh_from_db()
    assert not msg.is_read
    client.force_login(orga)  # orga may manage (support case) too
    assert client.get(_url("audio", event, msg.pk)).status_code == 200
    assert client.post(_url("delete", event, msg.pk)).status_code == 302
    assert not Message.objects.filter(pk=msg.pk).exists()


def test_settings_view(client, event, user, other_user, member, ext_alice):
    client.force_login(other_user)
    assert client.get(_url("settings", event, ext_alice.pk)).status_code == 403
    client.force_login(user)
    assert client.get(_url("settings", event, ext_alice.pk)).status_code == 200
    greeting = SimpleUploadedFile("hello.wav", b"RIFF....", content_type="audio/wav")
    r = client.post(_url("settings", event, ext_alice.pk), {
        "enabled": "on", "pin": "2468", "email_delivery": "on", "email": "", "max_messages": 20, "greeting": greeting})
    assert r.status_code == 302, r.content.decode()[:500]
    mb = Mailbox.objects.get(extension=ext_alice)
    assert mb.pin == "2468" and mb.email_delivery and mb.max_messages == 20 and mb.greeting.name.endswith("hello.wav")
    from apps.pbx.models import VoicemailUser

    assert VoicemailUser.objects.get(context="dial-demo", mailbox="4242").password == "2468"
    r = client.post(_url("settings", event, ext_alice.pk), {"enabled": "on", "pin": "12", "max_messages": 20})
    assert r.status_code == 200 and "4 to 6 digits" in r.content.decode()


def test_orga_all_view(client, event, user, orga, member, ext_alice):
    services.store_message(event, "4242", "4300", "", 5)
    client.force_login(user)
    assert client.get(_url("all", event)).status_code == 403
    client.force_login(orga)
    r = client.get(_url("all", event))
    assert r.status_code == 200 and "4242" in r.content.decode() and "counts" in r.content.decode()


def test_feature_flag_off(client, event, user, member, settings):
    settings.DIAL_FEATURES = dict(settings.DIAL_FEATURES, voicemail=False)
    client.force_login(user)
    assert client.get(_url("index", event)).status_code == 404


def test_api(event, user, other_user, orga, member, ext_alice, ext_bob, wav_file, pbx):
    c = _client(user)
    r = c.get("/api/v1/voicemail/mailboxes/?event=demo")
    assert r.status_code == 200 and r.json()["count"] == 1 and r.json()["results"][0]["number"] == "4242"
    assert "pin" not in r.json()["results"][0]
    mid = r.json()["results"][0]["id"]
    r = c.patch(f"/api/v1/voicemail/mailboxes/{mid}/", {"pin": "1357", "email_delivery": True}, format="json")
    assert r.status_code == 200 and r.json()["email_delivery"] is True
    assert Mailbox.objects.get(pk=mid).pin == "1357"
    assert c.patch(f"/api/v1/voicemail/mailboxes/{mid}/", {"pin": "1"}, format="json").status_code == 400
    # bob can't see or edit alice's box
    r = _client(other_user).patch(f"/api/v1/voicemail/mailboxes/{mid}/", {"pin": "9999"}, format="json")
    assert r.status_code == 404
    # orga sees all boxes of the event
    services.ensure_mailbox(ext_bob)
    assert _client(orga).get("/api/v1/voicemail/mailboxes/?event=demo").json()["count"] == 2

    msg = services.store_message(event, "4242", "4300", str(wav_file), 5)
    services.store_message(event, "4300", "4242", "", 5)
    r = c.get("/api/v1/voicemail/messages/?event=demo")
    assert r.json()["count"] == 1 and r.json()["results"][0]["caller_number"] == "4300"
    assert r.json()["results"][0]["audio_url"].endswith(f"/api/v1/voicemail/messages/{msg.pk}/audio/")
    assert c.get("/api/v1/voicemail/messages/?unread=1").json()["count"] == 1
    # orga does not get to read other people's messages
    assert _client(orga).get("/api/v1/voicemail/messages/?event=demo").json()["count"] == 0
    r = c.get(f"/api/v1/voicemail/messages/{msg.pk}/audio/")
    assert r.status_code == 200 and r["Content-Type"] == "audio/wav"
    assert c.post(f"/api/v1/voicemail/messages/{msg.pk}/mark-read/").json()["is_read"] is True
    assert c.get("/api/v1/voicemail/messages/?unread=1").json()["count"] == 0
    assert c.post(f"/api/v1/voicemail/messages/{msg.pk}/mark-unread/").json()["is_read"] is False
    assert _client(other_user).delete(f"/api/v1/voicemail/messages/{msg.pk}/").status_code == 404
    assert c.delete(f"/api/v1/voicemail/messages/{msg.pk}/").status_code == 204
    assert not Message.objects.filter(pk=msg.pk).exists()
