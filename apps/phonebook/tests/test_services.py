"""Phonebook services: entry selection/search and the exporters."""
import pytest
from django.urls import reverse

from apps.phonebook import services
from apps.phonebook.export import export_event

pytestmark = pytest.mark.django_db


def test_entries_filters_and_search(event, ext_alice, ext_bob, ext_hidden, ext_group):
    numbers = [e.number for e in services.entries(event)]
    assert numbers == ["4242", "4300", "4400"]  # hidden one excluded, group listed
    assert [e.number for e in services.entries(event, q="bob")] == ["4300"]  # owner nickname
    assert [e.number for e in services.entries(event, q="Builder")] == ["4300"]  # display name
    assert [e.number for e in services.entries(event, q="hackcenter")] == ["4242"]  # location
    assert [e.number for e in services.entries(event, q="43")] == ["4300"]
    assert [e.number for e in services.entries(event, type="group")] == ["4400"]


def test_as_dicts_respects_privacy_settings(event, ext_alice):
    d = services.as_dicts(event)[0]
    assert d["number"] == "4242" and d["name"] == "alice" and d["owner"] == "alice"
    assert d["location"] == "Hackcenter, table 12"
    s = services.get_settings(event)
    s.show_owner = False
    s.show_location = False
    s.categories = [{"name": "Users", "prefix": "4"}]
    s.save()
    d = services.as_dicts(event)[0]
    assert "owner" not in d and "location" not in d and d["category"] == "Users"


def test_csv_and_json(event, ext_alice, ext_bob):
    body = services.render_csv(event).decode()
    lines = body.strip().splitlines()
    assert lines[0].startswith("number,name,type") and "4242,alice,dect,alice" in lines[1]
    assert "4300,Bob Builder" in lines[2]
    assert [d["number"] for d in services.render_json(event)] == ["4242", "4300"]


def test_vcf(event, ext_alice, ext_bob):
    body = services.render_vcf(event).decode()
    assert body.count("BEGIN:VCARD") == 2 and "VERSION:3.0" in body
    assert "TEL;TYPE=WORK,VOICE:4242" in body and "FN:Bob Builder" in body
    assert "NOTE:Hackcenter\\, table 12" in body
    assert "ORG:Demo Camp" in body and body.endswith("END:VCARD\r\n")


def test_ldif(event, ext_alice, ext_bob, ext_group):
    body = services.render_ldif(event).decode()
    assert services.base_dn(event) == "ou=phonebook,dc=demo,dc=dial"
    assert body.startswith("dn: ou=phonebook,dc=demo,dc=dial\n")
    assert "dn: telephoneNumber=4242,ou=phonebook,dc=demo,dc=dial" in body
    assert "objectClass: inetOrgPerson" in body and "telephoneNumber: 4300" in body
    assert "cn: Bob Builder" in body and "sn: Builder" in body and "givenName: Bob" in body
    assert "cn: Infodesk" in body
    # non-ascii values are base64 encoded per RFC 2849
    ext_alice.display_name = "Zoë Ünïcode"
    ext_alice.save()
    body = services.render_ldif(event).decode()
    assert "cn:: " in body


def test_pdf_bytes(event, ext_alice, ext_bob):
    s = services.get_settings(event)
    s.intro_text = "Dial 9999 for voicemail"
    s.categories = [{"name": "Participants", "prefix": "4"}]
    s.save()
    body = services.render_pdf(event)
    assert body[:5] == b"%PDF-" and len(body) > 1000
    # the entries are drawn via reportlab; the text is stream-compressed so only check the structure
    assert b"/Page" in body and b"%%EOF" in body.strip()[-20:]
    assert services.render_pdf(event, exts=[])[:5] == b"%PDF-"


def test_trunk_block_shows_number_label(client, event, orga, ext_alice):
    from apps.extensions.services import register

    trunk = register(event, orga, "4700", "trunk", display_name="Village PBX", config={"block_digits": 2})
    assert trunk.state == "active"
    by_number = {d["number"]: d for d in services.as_dicts(event)}
    assert by_number["4700"]["number_label"] == "4700–4799"
    assert by_number["4242"]["number_label"] == "4242"
    assert [d["number_label"] for d in services.render_json(event)] == ["4242", "4700–4799"]
    html = client.get(reverse("phonebook:index", args=[event.slug])).content.decode()
    assert "4700–4799" in html and 'href="tel:4700"' in html
    assert services.render_pdf(event)[:5] == b"%PDF-"


def test_export_helper_and_event_export(event, ext_alice):
    assert export_event(event) == {"phonebook_settings": None}
    body, ctype, fname = services.export(event, "csv")
    assert ctype.startswith("text/csv") and fname == "phonebook-demo.csv" and b"4242" in body
    s = services.get_settings(event)
    s.intro_text = "hi"
    s.save()
    assert export_event(event)["phonebook_settings"]["intro_text"] == "hi"


def test_management_command(event, ext_alice, tmp_path):
    from django.core.management import call_command

    out = tmp_path / "pb.ldif"
    call_command("phonebook_ldif", event="demo", out=str(out))
    assert b"telephoneNumber: 4242" in out.read_bytes()
