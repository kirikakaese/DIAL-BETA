"""Remote phonebook (feature #9): vendor XML directories, token handling, prov-token variant, orga UI, API, CLI."""
import xml.etree.ElementTree as ET
from unittest import mock

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.api import cli
from apps.devices.models import Device, DeviceBinding
from apps.devices.provisioning_templates import load_builtin_profiles
from apps.events.export import export_event
from apps.extensions.services import register
from apps.phonebook import remote, services

pytestmark = [pytest.mark.django_db, pytest.mark.urls("apps.callback.tests.urls_stub")]

PUBLIC = "https://pet.example.org"


@pytest.fixture
def pb(event, settings):
    settings.PET_PUBLIC_URL = PUBLIC + "/"
    return services.get_settings(event)


def _url(event, token, vendor):
    return reverse("phonebook:remote_directory", args=[event.slug, token, vendor])


def _entries(tree, vendor):
    if vendor == "grandstream":
        return [(c.findtext("FirstName"), c.findtext("LastName"), c.find("Phone/phonenumber").text)
                for c in tree.findall("Contact")]
    return [(e.findtext("Name"), e.findtext("Telephone")) for e in tree.findall("DirectoryEntry")]


def test_every_vendor_renders_valid_xml(client, event, pb, ext_alice, ext_bob, ext_hidden, ext_group):
    roots = {"snom": "SnomIPPhoneDirectory", "yealink": "YealinkIPPhoneDirectory", "grandstream": "AddressBook",
             "cisco": "CiscoIPPhoneDirectory", "mitel": "IPPhoneDirectory", "generic": "IPPhoneDirectory"}
    assert set(roots) == set(remote.VENDORS)
    for vendor, root in roots.items():
        r = client.get(_url(event, pb.directory_token, vendor))
        assert r.status_code == 200, vendor
        assert r["Content-Type"] == "text/xml; charset=utf-8"
        assert r["Cache-Control"] == "no-store" and r["X-Robots-Tag"] == "noindex"
        tree = ET.fromstring(r.content)
        assert tree.tag == root
        body = r.content.decode()
        assert "4242" in body and "4300" in body and "4400" in body and "4301" not in body
        if vendor == "grandstream":
            assert ("Bob", "Builder", "4300") in _entries(tree, vendor)
            assert ("", "Infodesk", "4400") in _entries(tree, vendor)
            assert tree.find("Contact/Phone").get("type") == "Work"
            assert tree.find("Contact/Phone/accountindex").text == "0"
        else:
            assert ("Bob Builder", "4300") in _entries(tree, vendor)
            assert ("alice", "4242") in _entries(tree, vendor)  # no display name -> owner nickname
    cisco = ET.fromstring(client.get(_url(event, pb.directory_token, "cisco")).content)
    assert cisco.findtext("Title") == "Demo Camp" and cisco.find("Prompt") is not None
    assert ET.fromstring(client.get(_url(event, pb.directory_token, "yealink")).content).find("Prompt") is None


def test_search_filters_all_aliases(client, event, pb, ext_alice, ext_bob):
    url = _url(event, pb.directory_token, "mitel")
    for param in ("q", "search", "name"):
        tree = ET.fromstring(client.get(url + f"?{param}=bob").content)
        assert _entries(tree, "mitel") == [("Bob Builder", "4300")], param
    tree = ET.fromstring(client.get(url + "?q=nomatch").content)
    assert tree.findall("DirectoryEntry") == [] and tree.tag == "IPPhoneDirectory"
    gs = ET.fromstring(client.get(_url(event, pb.directory_token, "grandstream") + "?q=42").content)
    assert [c.find("Phone/phonenumber").text for c in gs.findall("Contact")] == ["4242"]


def test_xml_escaping_and_trunk_label(client, event, pb, orga, admin):
    register(event, orga, "1300", "group", display_name="Info <&> \"Desk\"")
    ext = register(event, admin, "4700", "trunk", display_name="Remote PBX", config={"block_digits": 2},
                   force_active=True)
    assert ext.state == "active" and ext.number_label == "4700–4799"
    tree = ET.fromstring(client.get(_url(event, pb.directory_token, "snom")).content)
    names = dict(_entries(tree, "snom"))
    assert names['Info <&> "Desk"'] == "1300"
    assert names["Remote PBX (4700–4799)"] == "4700"  # range in the name, dialable number in <Telephone>


def test_wrong_token_disabled_or_unknown_are_404(client, event, pb, ext_alice, settings):
    good = _url(event, pb.directory_token, "snom")
    assert client.get(good).status_code == 200
    assert client.get(_url(event, "x" * 32, "snom")).status_code == 404
    assert client.get(_url(event, pb.directory_token, "polycom")).status_code == 404
    assert client.get(_url(event, pb.directory_token, "snom").replace("/demo/", "/nope/")).status_code == 404
    assert client.post(good).status_code == 405
    pb.directory_enabled = False
    pb.save()
    assert client.get(good).status_code == 404
    pb.directory_enabled = True
    pb.save()
    settings.PET_FEATURES = dict(settings.PET_FEATURES, phonebook=False)
    assert client.get(good).status_code == 404


def test_private_event_still_serves_with_token_but_not_draft_or_archived(client, event, pb, ext_alice):
    """Visibility is irrelevant (the token is the credential) but the lifecycle is: phones only get the
    directory while the event is open - the same rule the LDAP server applies."""
    url = _url(event, pb.directory_token, "generic")
    event.is_public = False
    event.save()
    assert client.get(url).status_code == 200
    for state in ("draft", "archived"):
        event.state = state
        event.save()
        assert client.get(url).status_code == 404, state
    event.state = "live"
    event.save()
    assert client.get(url).status_code == 200


def test_directory_urls_and_ldap_info(event, pb, settings):
    urls = remote.directory_urls(event)
    assert set(urls) == set(remote.VENDORS)
    assert urls["snom"] == f"{PUBLIC}/e/demo/phonebook/remote/{pb.directory_token}/snom.xml"
    assert remote.directory_url(event, "mitel").endswith("/mitel.xml")
    info = remote.ldap_info(event)
    assert info == {"host": "pet.example.org", "port": 3890, "base_dn": "ou=phonebook,dc=demo,dc=pet",
                    "bind_dn": "cn=directory,dc=demo,dc=pet", "name_attributes": "cn sn",
                    "number_attribute": "telephoneNumber"}
    settings.PET_LDAP_PORT = 389
    settings.PET_LDAP_HOST = "ldap.camp.local"
    info = remote.directory_info(event)
    assert info["ldap"]["port"] == 389 and info["ldap"]["host"] == "ldap.camp.local"
    assert info["ldap"]["password"] == info["token"] == pb.directory_token and info["enabled"] is True
    assert remote.directory_info(event, with_token=False)["token"] is None


# --------------------------------------------------------------------------- provisioning-token variant

@pytest.fixture
def phone(event, user, member, ext_alice, ext_bob):
    load_builtin_profiles()
    from apps.devices.models import ProvisioningProfile

    d = Device.objects.create(event=event, owner=user, type="sip", mac_address="00:11:22:aa:bb:cc",
                              provisioning_profile=ProvisioningProfile.objects.get(vendor="grandstream", event=None))
    d.ensure_sip_credentials()
    DeviceBinding.objects.create(extension=ext_alice, device=d)
    return d


def test_prov_phonebook_picks_vendor_from_profile(client, phone, pb, ext_hidden):
    url = f"/prov/{phone.provisioning_token}/phonebook.xml"
    assert url == reverse("prov:phonebook", args=[phone.provisioning_token])
    r = client.get(url)
    assert r.status_code == 200 and r["Content-Type"] == "text/xml; charset=utf-8"
    assert "no-store" in r["Cache-Control"] and r["X-Robots-Tag"] == "noindex"
    tree = ET.fromstring(r.content)
    assert tree.tag == "AddressBook" and b"4301" not in r.content
    assert ET.fromstring(client.get(url + "?vendor=yealink").content).tag == "YealinkIPPhoneDirectory"
    assert ET.fromstring(client.get(url + "?vendor=bogus").content).tag == "AddressBook"
    assert _entries(ET.fromstring(client.get(url + "?vendor=snom&q=bob").content), "snom") == [("Bob Builder", "4300")]
    # devices without a vendor profile (softphones) get the generic schema
    phone.provisioning_profile = None
    phone.save()
    assert ET.fromstring(client.get(url).content).tag == "IPPhoneDirectory"


def test_prov_phonebook_404s(client, phone, pb, settings):
    url = f"/prov/{phone.provisioning_token}/phonebook.xml"
    assert client.get("/prov/not-a-real-token-at-all-xxxxxxxx/phonebook.xml").status_code == 404
    assert client.get("/prov/short/phonebook.xml").status_code == 404
    pb.directory_enabled = False
    pb.save()
    assert client.get(url).status_code == 404
    pb.directory_enabled = True
    pb.save()
    phone.state = "disabled"
    phone.save()
    assert client.get(url).status_code == 404
    phone.state = "new"
    phone.save()
    assert client.get(url).status_code == 200
    settings.PET_FEATURES = dict(settings.PET_FEATURES, phonebook=False)
    assert client.get(url).status_code == 404


def test_builtin_templates_carry_the_phonebook_url(phone, pb):
    from apps.devices.models import ProvisioningProfile

    profiles = {p.vendor: p for p in ProvisioningProfile.objects.filter(event=None)}
    prov_url = f"{PUBLIC}/prov/{phone.provisioning_token}/phonebook.xml"
    assert remote.prov_directory_url(phone) == prov_url
    snom = profiles["snom"].render(phone)
    assert f'<dkey_directory perm="">url {prov_url}</dkey_directory>' in snom
    yealink = profiles["yealink"].render(phone)
    assert f"remote_phonebook.data.1.url = {prov_url}" in yealink
    assert "remote_phonebook.data.1.name = Demo Camp" in yealink and "features.remote_phonebook.enable = 1" in yealink
    gs = profiles["grandstream"].render(phone)
    assert "<P330>3</P330>" in gs  # HTTPS
    assert f"<P331>pet.example.org/prov/{phone.provisioning_token}</P331>" in gs  # folder, phone appends the file
    assert "<P332>60</P332>" in gs
    cisco = profiles["cisco"].render(phone)
    assert f"<XML_Directory_Service_URL>{prov_url}</XML_Directory_Service_URL>" in cisco
    assert "<XML_Directory_Service_Name>Demo Camp</XML_Directory_Service_Name>" in cisco
    for body in (snom, gs, cisco):
        ET.fromstring(body.encode())
    # directory off -> no phonebook lines at all (and no dangling keys)
    pb.directory_enabled = False
    pb.save()
    assert remote.prov_directory_url(phone) == ""
    for vendor in ("snom", "yealink", "grandstream", "cisco"):
        body = profiles[vendor].render(phone)
        assert "phonebook" not in body and "P330" not in body and "dkey_directory" not in body, vendor


def test_load_builtin_profiles_update_rewrites_existing_rows():
    from apps.devices.models import ProvisioningProfile

    assert load_builtin_profiles() == 4
    p = ProvisioningProfile.objects.get(name="Snom (built-in)", event=None)
    p.template = "custom"
    p.save()
    assert load_builtin_profiles() == 0
    p.refresh_from_db()
    assert p.template == "custom"
    assert load_builtin_profiles(update=True) == 0
    p.refresh_from_db()
    assert "dkey_directory" in p.template


# --------------------------------------------------------------------------- orga UI

def test_settings_page_lists_urls_and_ldap(client, event, pb, orga):
    client.force_login(orga)
    r = client.get(reverse("phonebook:settings", args=[event.slug]))
    assert r.status_code == 200
    html = r.content.decode()
    for vendor in remote.VENDORS:
        assert remote.directory_url(event, vendor) in html, vendor
    assert "cn=directory,dc=demo,dc=pet" in html and "ou=phonebook,dc=demo,dc=pet" in html
    assert "3890" in html and pb.directory_token in html
    assert reverse("phonebook:rotate_directory_token", args=[event.slug]) in html
    assert 'name="directory_enabled"' in html


def test_settings_form_toggles_directory(client, event, pb, orga):
    client.force_login(orga)
    url = reverse("phonebook:settings", args=[event.slug])
    r = client.post(url, {"intro_text": "", "show_location": "on", "show_owner": "on", "categories": "[]"})
    assert r.status_code == 302
    pb.refresh_from_db()
    assert pb.directory_enabled is False
    assert "disabled" in client.get(url).content.decode()
    client.post(url, {"intro_text": "", "show_location": "on", "show_owner": "on", "categories": "[]",
                      "directory_enabled": "on"})
    pb.refresh_from_db()
    assert pb.directory_enabled is True


def test_rotate_requires_orga_and_changes_token(client, event, pb, user, member, orga, ext_alice):
    url = reverse("phonebook:rotate_directory_token", args=[event.slug])
    old = pb.directory_token
    client.force_login(user)
    assert client.post(url).status_code == 403
    client.force_login(orga)
    assert client.get(url).status_code == 405
    r = client.post(url)
    assert r.status_code == 302 and r["Location"] == reverse("phonebook:settings", args=[event.slug])
    pb.refresh_from_db()
    assert pb.directory_token != old and len(pb.directory_token) >= 24
    assert client.get(_url(event, old, "snom")).status_code == 404
    assert client.get(_url(event, pb.directory_token, "snom")).status_code == 200
    from apps.core.models import AuditLog

    entry = AuditLog.objects.filter(event=event, message__icontains="token rotated").first()
    assert entry is not None and entry.actor == orga and old not in str(entry.changes)


def test_token_not_exported_or_cloned(event, pb, admin):
    data = export_event(event)
    assert data["phonebook_settings"]["directory_enabled"] is True
    assert pb.directory_token not in str(data)
    new = event.clone(name="Next", slug="next", start_date=event.start_date, end_date=event.end_date, actor=admin)
    assert services.get_settings(new).directory_token != pb.directory_token


# --------------------------------------------------------------------------- API + CLI

def test_api_directory_orga_only(event, pb, user, member, orga, ext_alice):
    c = APIClient()
    assert c.get("/api/v1/phonebook/directory/?event=demo").status_code in (401, 403)
    c.force_authenticate(user)
    assert c.get("/api/v1/phonebook/directory/?event=demo").status_code == 403
    assert c.post("/api/v1/phonebook/directory/rotate/?event=demo").status_code == 403
    c.force_authenticate(orga)
    assert c.get("/api/v1/phonebook/directory/").status_code == 400
    assert c.get("/api/v1/phonebook/directory/?event=nope").status_code == 400
    r = c.get("/api/v1/phonebook/directory/?event=demo")
    assert r.status_code == 200
    data = r.json()
    assert data["event"] == "demo" and data["enabled"] is True and data["token"] == pb.directory_token
    assert set(data["urls"]) == set(remote.VENDORS) and data["urls"]["yealink"].endswith("/yealink.xml")
    assert data["ldap"]["bind_dn"] == "cn=directory,dc=demo,dc=pet" and data["ldap"]["password"] == pb.directory_token
    r = c.post("/api/v1/phonebook/directory/rotate/", {"event": "demo"}, format="json")
    assert r.status_code == 200 and r.json()["token"] != pb.directory_token
    pb.refresh_from_db()
    assert r.json()["token"] == pb.directory_token


def test_api_directory_service_account_scope(event, pb, orga, ext_alice):
    from apps.accounts.models import ServiceAccount

    c = APIClient()
    _acct, raw = ServiceAccount.issue(name="ro", owner=orga, event=event, scopes=["phonebook:read"])
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert c.get("/api/v1/phonebook/directory/?event=demo").status_code == 200
    assert c.post("/api/v1/phonebook/directory/rotate/?event=demo").status_code == 403


class _Resp:
    def __init__(self, data, status_code=200):
        self._data, self.status_code, self.content = data, status_code, b"x"

    def json(self):
        return self._data


def test_cli_phonebook_directory(capsys):
    payload = {"event": "demo", "enabled": True, "token": "tok", "urls": {"snom": "https://p/snom.xml"},
               "ldap": {"host": "p", "port": 3890, "base_dn": "ou=phonebook,dc=demo,dc=pet",
                        "bind_dn": "cn=directory,dc=demo,dc=pet", "password": "tok"}}
    with mock.patch("requests.Session.request", return_value=_Resp(payload)) as req:
        assert cli.main(["--url", "http://pet.test", "phonebook", "directory", "--event", "demo"]) == 0
    assert req.call_args.args[:2] == ("GET", "http://pet.test/api/v1/phonebook/directory/")
    assert req.call_args.kwargs["params"] == {"event": "demo"}
    out = capsys.readouterr().out
    assert "https://p/snom.xml" in out and "cn=directory,dc=demo,dc=pet" in out and "token:   tok" in out
    with mock.patch("requests.Session.request", return_value=_Resp(payload)) as req:
        assert cli.main(["--url", "http://pet.test", "--format", "json", "phonebook", "directory", "--event", "demo",
                         "--rotate"]) == 0
    assert req.call_args.args[:2] == ("POST", "http://pet.test/api/v1/phonebook/directory/rotate/")
    assert req.call_args.kwargs["params"] == {"event": "demo"}
    assert '"token": "tok"' in capsys.readouterr().out
    # the plain listing still works
    with mock.patch("requests.Session.request", return_value=_Resp({"results": []})) as req:
        assert cli.main(["--url", "http://pet.test", "phonebook", "--event", "demo"]) == 0
    assert req.call_args.args[:2] == ("GET", "http://pet.test/api/v1/phonebook/")
