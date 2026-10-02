"""Phonebook views + API: public access, login redirect for private events, exports."""
import pytest
from django.urls import reverse
from rest_framework.test import APIClient

pytestmark = [pytest.mark.django_db, pytest.mark.urls("apps.callback.tests.urls_stub")]


def _url(name, event):
    return reverse(f"phonebook:{name}", args=[event.slug])


def test_public_event_needs_no_login(client, event, ext_alice, ext_bob, ext_hidden):
    r = client.get(_url("index", event))
    assert r.status_code == 200
    body = r.content.decode()
    assert "4242" in body and "4300" in body and "4301" not in body
    assert "ou=phonebook,dc=demo,dc=dial" in body
    r = client.get(_url("index", event) + "?q=bob")
    assert "4300" in r.content.decode() and "4242" not in r.content.decode().split("<tbody>")[1]


def test_private_event_redirects_anonymous(client, event, user, member, ext_alice):
    event.is_public = False
    event.save()
    r = client.get(_url("index", event))
    assert r.status_code == 302 and "login" in r["Location"]
    client.force_login(user)
    assert client.get(_url("index", event)).status_code == 200


def test_exports(client, event, ext_alice):
    r = client.get(_url("pdf", event))
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf" and r.content[:5] == b"%PDF-"
    r = client.get(_url("csv", event))
    assert r.status_code == 200 and b"4242" in r.content and "attachment" in r["Content-Disposition"]
    r = client.get(_url("vcf", event))
    assert r.status_code == 200 and b"BEGIN:VCARD" in r.content
    r = client.get(_url("ldif", event))
    assert r.status_code == 200 and b"telephoneNumber: 4242" in r.content
    r = client.get(_url("json", event))
    assert r.status_code == 200 and r.json()["entries"][0]["number"] == "4242"


def test_settings_view_orga_only(client, event, user, member, orga):
    client.force_login(user)
    assert client.get(_url("settings", event)).status_code == 403
    client.force_login(orga)
    assert client.get(_url("settings", event)).status_code == 200
    r = client.post(_url("settings", event), {"intro_text": "Welcome", "show_location": "on", "categories": "[]"})
    assert r.status_code == 302
    assert "Welcome" in client.get(_url("index", event)).content.decode()


def test_feature_flag_off(client, event, settings, ext_alice):
    settings.DIAL_FEATURES = dict(settings.DIAL_FEATURES, phonebook=False)
    assert client.get(_url("index", event)).status_code == 404


def test_api_list_and_export(event, user, member, ext_alice, ext_hidden):
    c = APIClient()
    r = c.get("/api/v1/phonebook/?event=demo")
    assert r.status_code == 200 and r.json()["count"] == 1 and r.json()["results"][0]["number"] == "4242"
    assert c.get("/api/v1/phonebook/?event=demo&q=nomatch").json()["count"] == 0
    assert c.get("/api/v1/phonebook/").status_code == 400
    r = c.get("/api/v1/phonebook/export.vcf?event=demo")
    assert r.status_code == 200 and b"TEL;TYPE=WORK,VOICE:4242" in r.content
    assert c.get("/api/v1/phonebook/export.xyz?event=demo").status_code == 404

    event.is_public = False
    event.save()
    assert c.get("/api/v1/phonebook/?event=demo").status_code == 401
    c.force_authenticate(user)
    assert c.get("/api/v1/phonebook/?event=demo").status_code == 200
