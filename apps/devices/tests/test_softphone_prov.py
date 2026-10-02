"""Softphone QR onboarding: Linphone / Acrobits provisioning XML, ``softphone_links()``, QR view, UI, API."""
import pytest
from django.urls import reverse

from apps.devices.models import Device, DeviceBinding
from apps.devices.softphone import render_acrobits, render_linphone
from apps.extensions import services as ext_services

pytestmark = pytest.mark.django_db

PUBLIC = "https://pet.example.org"


@pytest.fixture
def softphone(event, user, member, settings):
    settings.PET_PUBLIC_URL = PUBLIC + "/"
    ext = ext_services.register(event, user, "4242", "sip", display_name="Alice & Co")
    d = Device.objects.create(event=event, owner=user, type="sip", sip_transport="tls")
    d.ensure_sip_credentials()
    DeviceBinding.objects.create(extension=ext, device=d)
    return d


@pytest.fixture
def handset(event, user, member):
    d = Device.objects.create(event=event, owner=user, type="dect", ipei="0123456789012")
    d.ensure_sip_credentials()
    return d


def test_softphone_links(softphone):
    links = softphone.softphone_links()
    tok = softphone.provisioning_token
    assert set(links) == {"generic", "linphone", "acrobits"}
    assert links["generic"] == softphone.softphone_deeplink() == (
        f"sip:{softphone.sip_username}:{softphone.sip_password}@demo.pet.local;transport=tls")
    assert links["linphone"] == f"linphone-config:{PUBLIC}/prov/{tok}/linphone.xml"
    assert links["acrobits"] == f"{PUBLIC}/prov/{tok}/acrobits.xml"
    assert reverse("prov:softphone", args=[tok, "linphone"]) == f"/prov/{tok}/linphone.xml"
    # no token yet -> only the generic deep link is available
    bare = Device(event=softphone.event, type="sip", sip_username="u", sip_password="p")
    assert bare.softphone_links() == {"generic": "sip:u:p@demo.pet.local;transport=udp", "linphone": "",
                                      "acrobits": ""}


def test_linphone_xml_endpoint(client, softphone):
    r = client.get(f"/prov/{softphone.provisioning_token}/linphone.xml")
    assert r.status_code == 200
    assert r["Content-Type"] == "application/xml; charset=utf-8"
    assert "no-store" in r["Cache-Control"] and r["X-Robots-Tag"] == "noindex"
    body = r.content.decode()
    assert '<config xmlns="http://www.linphone.org/xsds/lpconfig.xsd"' in body
    assert '<section name="proxy_0">' in body and '<section name="auth_info_0">' in body
    assert '<section name="sip">' in body
    assert ('<entry name="reg_identity">"Alice &amp; Co" &lt;sip:' + softphone.sip_username
            + "@demo.pet.local&gt;</entry>") in body
    assert '<entry name="reg_proxy">&lt;sip:demo.pet.local;transport=tls&gt;</entry>' in body
    assert '<entry name="reg_route">&lt;sip:demo.pet.local;transport=tls&gt;</entry>' in body
    assert '<entry name="reg_expires">600</entry>' in body
    assert '<entry name="publish">0</entry>' in body and '<entry name="dial_escape_plus">0</entry>' in body
    assert f'<entry name="username">{softphone.sip_username}</entry>' in body
    assert f'<entry name="passwd">{softphone.sip_password}</entry>' in body
    assert '<entry name="realm">demo.pet.local</entry>' in body
    assert '<entry name="domain">demo.pet.local</entry>' in body
    # TLS device: only the TLS transport is enabled
    assert '<entry name="sip_tls_port">-1</entry>' in body and '<entry name="sip_port">0</entry>' in body
    softphone.refresh_from_db()
    assert softphone.config["last_provisioned_client"] == "linphone" and softphone.config["last_provisioned_at"]


def test_acrobits_xml_endpoint(client, softphone):
    r = client.get(f"/prov/{softphone.provisioning_token}/acrobits.xml")
    assert r.status_code == 200 and r["Content-Type"] == "application/xml; charset=utf-8"
    body = r.content.decode()
    assert body.lstrip().startswith('<?xml version="1.0" encoding="UTF-8"?>\n<account>')
    assert "<title>PET 4242</title>" in body
    assert f"<username>{softphone.sip_username}</username>" in body
    assert f"<password>{softphone.sip_password}</password>" in body
    assert "<host>demo.pet.local</host>" in body
    assert "<transport>tls</transport>" in body
    assert "<displayName>Alice &amp; Co</displayName>" in body
    softphone.refresh_from_db()
    assert softphone.config["last_provisioned_client"] == "acrobits"


def test_transport_mapping(softphone):
    softphone.sip_transport = "udp"
    assert "<transport>udp</transport>" in render_acrobits(softphone)
    assert '<entry name="sip_port">-1</entry>' in render_linphone(softphone)
    assert '<entry name="reg_proxy">&lt;sip:demo.pet.local;transport=udp&gt;</entry>' in render_linphone(softphone)
    softphone.sip_transport = "tcp"
    assert "<transport>tcp</transport>" in render_acrobits(softphone)
    softphone.sip_transport = "wss"  # no classic-SIP softphone speaks WSS; TLS is the closest match
    assert "<transport>tls</transport>" in render_acrobits(softphone)


def test_softphone_xml_wrong_token_or_device_404(client, softphone, handset):
    tok = softphone.provisioning_token
    assert client.get("/prov/not-a-real-token-at-all-xxxxxxxx/linphone.xml").status_code == 404
    assert client.get("/prov/short/acrobits.xml").status_code == 404
    assert client.get(f"/prov/{tok}/zoiper.xml").status_code == 404
    assert client.post(f"/prov/{tok}/linphone.xml").status_code == 405
    # a DECT handset's token serves nothing for softphones
    assert client.get(f"/prov/{handset.provisioning_token}/linphone.xml").status_code == 404
    softphone.state = "disabled"
    softphone.save()
    assert client.get(f"/prov/{tok}/linphone.xml").status_code == 404
    assert client.get(f"/prov/{tok}/acrobits.xml").status_code == 404
    softphone.state = "new"
    softphone.sip_password = ""
    softphone.save()
    assert client.get(f"/prov/{tok}/acrobits.xml").status_code == 404


def test_device_qr_clients(client, softphone, event, user, other_user):
    url = reverse("portal:device_qr", args=[event.slug, softphone.pk])
    client.force_login(user)
    for q in ("", "?client=generic", "?client=linphone", "?client=acrobits"):
        r = client.get(url + q)
        assert r.status_code == 200, q
        assert r["Content-Type"] == "image/png" and r.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert r["Cache-Control"] == "private, no-store"
    assert client.get(url + "?client=zoiper").status_code == 404
    client.force_login(other_user)
    assert client.get(url + "?client=linphone").status_code == 403


def test_device_qr_issues_missing_token(client, event, user, member):
    d = Device.objects.create(event=event, owner=user, type="sip", sip_username="demo-abc", sip_password="pw")
    assert d.provisioning_token == ""
    client.force_login(user)
    r = client.get(reverse("portal:device_qr", args=[event.slug, d.pk]) + "?client=linphone")
    assert r.status_code == 200
    d.refresh_from_db()
    assert d.provisioning_token and d.softphone_links()["linphone"].endswith("/linphone.xml")


def test_device_detail_shows_three_options_for_sip(client, softphone, event, user):
    client.force_login(user)
    r = client.get(reverse("portal:device_detail", args=[event.slug, softphone.pk]))
    assert r.status_code == 200
    html = r.content.decode()
    qr = reverse("portal:device_qr", args=[event.slug, softphone.pk])
    for c in ("linphone", "acrobits", "generic"):
        assert f"{qr}?client={c}" in html, c
    assert "Groundwire / Acrobits" in html and "Other softphone (SIP URI)" in html and ">Linphone<" in html
    assert "Fetch remote configuration" in html and "Scan QR code" in html
    assert "Manual settings" in html and "Zoiper" in html
    assert softphone.softphone_links()["linphone"] in html and softphone.softphone_links()["acrobits"] in html
    assert f"<code>{softphone.sip_port}</code>" in html and "Alice &amp; Co" in html


def test_device_detail_no_softphone_options_for_dect(client, handset, event, user):
    client.force_login(user)
    r = client.get(reverse("portal:device_detail", args=[event.slug, handset.pk]))
    assert r.status_code == 200
    html = r.content.decode()
    assert "?client=" not in html and "Manual settings" not in html and "Groundwire" not in html
    assert "DECT subscription" in html


def test_api_softphone_links_only_for_owner_or_orga(client, softphone, user, orga, other_user, admin, event):
    url = f"/api/v1/devices/{softphone.pk}/"
    client.force_login(user)
    data = client.get(url).json()
    assert data["softphone_links"] == softphone.softphone_links()
    assert data["sip_password"] == softphone.sip_password
    client.force_login(orga)
    assert client.get(url).json()["softphone_links"]["acrobits"].endswith("/acrobits.xml")
    # list endpoint never carries secrets
    rows = client.get("/api/v1/devices/?event__slug=demo").json()
    rows = rows["results"] if isinstance(rows, dict) else rows
    assert rows and all("softphone_links" not in row and "sip_password" not in row for row in rows)
    # unrelated user: not visible at all
    client.force_login(other_user)
    assert client.get(url).status_code in (403, 404)
    assert client.get("/api/v1/devices/?event__slug=demo").json()["count"] == 0
    # API QR endpoint honours ?client= too
    client.force_login(user)
    r = client.get(f"/api/v1/devices/{softphone.pk}/qr/?client=acrobits")
    assert r.status_code == 200 and r["Content-Type"] == "image/png"
    assert client.get(f"/api/v1/devices/{softphone.pk}/qr/?client=nope").status_code == 404


def test_api_softphone_links_null_for_dect(client, handset, user):
    client.force_login(user)
    data = client.get(f"/api/v1/devices/{handset.pk}/").json()
    assert data["softphone_links"] is None
