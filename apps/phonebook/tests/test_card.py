"""Business card per extension (feature #20): single vCard, vCard-QR, printable card, API fields."""
import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.events.models import EventMembership
from apps.extensions import services as ext_services
from apps.phonebook import services

pytestmark = [pytest.mark.django_db, pytest.mark.urls("apps.callback.tests.urls_stub")]

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _url(name, event, number):
    return reverse(f"phonebook:{name}", args=[event.slug, number])


def test_urls_resolve_as_specified(event):
    assert _url("vcard_one", event, "4242") == "/e/demo/phonebook/4242.vcf"
    assert _url("card_qr", event, "4242") == "/e/demo/phonebook/4242/qr.png"
    assert _url("card_print", event, "4242") == "/e/demo/phonebook/4242/card/"


def test_vcard_one_content(client, event, ext_alice):
    ext_services.update(ext_alice, None, display_name="Alice; Wonder", description="Ask me about DECT")
    r = client.get(_url("vcard_one", event, "4242"))
    assert r.status_code == 200
    assert r["Content-Type"].startswith("text/vcard")
    assert "attachment" in r["Content-Disposition"] and "demo-4242.vcf" in r["Content-Disposition"]
    assert r["Cache-Control"] == "private, no-store"
    body = r.content.decode()
    lines = body.split("\r\n")
    assert lines[0] == "BEGIN:VCARD" and lines[1] == "VERSION:3.0"
    assert "FN:Alice\\; Wonder" in lines
    assert "TEL;TYPE=WORK,VOICE:4242" in lines
    assert "ORG:Demo Camp" in lines
    assert "NOTE:Hackcenter\\, table 12 - Ask me about DECT" in lines
    assert "X-PET-EVENT:demo" in lines
    assert body.count("BEGIN:VCARD") == 1 and body.endswith("END:VCARD\r\n")


def test_bulk_export_still_one_card_per_entry(event, ext_alice, ext_bob):
    body = services.render_vcf(event).decode()
    assert body.count("BEGIN:VCARD") == 2 and body.count("X-PET-EVENT:demo") == 2


def test_card_qr_is_png_with_compact_vcard(client, event, ext_alice):
    r = client.get(_url("card_qr", event, "4242"))
    assert r.status_code == 200 and r["Content-Type"] == "image/png"
    assert r.content[:8] == PNG_MAGIC
    assert r["Cache-Control"] == "private, no-store"
    compact = services.render_vcard(event, ext_alice, compact=True)
    assert "FN:alice" in compact and "TEL;TYPE=WORK,VOICE:4242" in compact and "ORG:Demo Camp" in compact
    assert "NOTE:Hackcenter\\, table 12" in compact
    assert "UID:" not in compact and "CATEGORIES:" not in compact and "X-PET-EVENT" not in compact


def test_unknown_or_inactive_number_404(client, event, user, member, ext_alice):
    assert client.get(_url("vcard_one", event, "9876")).status_code == 404
    ext_services.suspend(ext_alice, actor=None, note="test")
    assert client.get(_url("vcard_one", event, "4242")).status_code == 404
    client.force_login(user)  # not even the owner
    assert client.get(_url("card_qr", event, "4242")).status_code == 404


def test_hidden_extension_visibility(client, event, user, member, other_user, orga, ext_hidden):
    # anonymous / unrelated member: 404 (do not leak that the number exists)
    for name in ("vcard_one", "card_qr", "card_print"):
        assert client.get(_url(name, event, "4301")).status_code == 404
    client.force_login(user)
    for name in ("vcard_one", "card_qr", "card_print"):
        assert client.get(_url(name, event, "4301")).status_code == 404
    # owner
    client.force_login(other_user)
    for name in ("vcard_one", "card_qr", "card_print"):
        assert client.get(_url(name, event, "4301")).status_code == 200
    # orga
    client.force_login(orga)
    assert client.get(_url("vcard_one", event, "4301")).status_code == 200
    # helpdesk
    EventMembership.objects.filter(event=event, user=user).update(role="helpdesk")
    client.force_login(user)
    assert client.get(_url("vcard_one", event, "4301")).status_code == 200


def test_print_page_renders_branding_and_qr(client, event, ext_bob):
    event.primary_color = "#aa00bb"
    event.save()
    r = client.get(_url("card_print", event, "4300"))
    assert r.status_code == 200
    body = r.content.decode()
    assert "Bob Builder" in body and "4300" in body and "Demo Camp" in body
    assert "#aa00bb" in body
    assert _url("card_qr", event, "4300") in body and _url("vcard_one", event, "4300") in body
    assert "Scan to save this number" in body
    assert "@page" in body


def test_feature_flag_off_hides_card_routes(client, event, settings, ext_alice):
    settings.PET_FEATURES = dict(settings.PET_FEATURES, phonebook=False)
    assert client.get(_url("vcard_one", event, "4242")).status_code == 404
    assert client.get(_url("card_qr", event, "4242")).status_code == 404


def test_extension_detail_shows_business_card(client, event, user, member, ext_alice):
    client.force_login(user)
    r = client.get(reverse("portal:extension_detail", args=[event.slug, ext_alice.pk]))
    assert r.status_code == 200
    body = r.content.decode()
    assert "Business card" in body and "Scan to save this number" in body
    assert _url("card_qr", event, "4242") in body
    assert _url("vcard_one", event, "4242") in body
    assert _url("card_print", event, "4242") in body


def test_extension_detail_no_card_when_not_active(client, event, user, member, ext_alice):
    ext_services.suspend(ext_alice, actor=None, note="test")
    client.force_login(user)
    r = client.get(reverse("portal:extension_detail", args=[event.slug, ext_alice.pk]))
    assert r.status_code == 200 and "Business card" not in r.content.decode()


def test_phonebook_index_links_vcard_per_row(client, event, ext_alice, ext_bob):
    body = client.get(reverse("phonebook:index", args=[event.slug])).content.decode()
    assert _url("vcard_one", event, "4242") in body and _url("vcard_one", event, "4300") in body


def test_api_exposes_card_urls(event, settings, ext_alice):
    settings.PET_PUBLIC_URL = "https://pet.example.org/"
    r = APIClient().get("/api/v1/phonebook/?event=demo")
    assert r.status_code == 200
    entry = r.json()["results"][0]
    assert entry["vcard_url"] == "https://pet.example.org/e/demo/phonebook/4242.vcf"
    assert entry["card_qr_url"] == "https://pet.example.org/e/demo/phonebook/4242/qr.png"
