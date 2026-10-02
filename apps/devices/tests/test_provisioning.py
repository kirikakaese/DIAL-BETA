"""Served autoprovisioning: token URLs, MAC lookup with token/basic auth, built-in vendor templates."""
import base64

import pytest
from django.urls import reverse

from apps.devices.models import Device, DeviceBinding, ProvisioningProfile
from apps.devices.provisioning_templates import BUILTIN_PROFILES, load_builtin_profiles
from apps.extensions import services as ext_services

pytestmark = pytest.mark.django_db

MAC = "00:11:22:aa:bb:cc"


@pytest.fixture
def profiles(db):
    load_builtin_profiles()
    return {p.vendor: p for p in ProvisioningProfile.objects.filter(event=None)}


@pytest.fixture
def phone(event, user, member, profiles):
    ext = ext_services.register(event, user, "4711", "sip", display_name="Alice & Co")
    d = Device.objects.create(event=event, owner=user, type="sip", mac_address=MAC,
                              provisioning_profile=profiles["yealink"])
    d.ensure_sip_credentials()
    DeviceBinding.objects.create(extension=ext, device=d)
    return d


def _basic(user, pw):
    return {"HTTP_AUTHORIZATION": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_builtin_profiles_idempotent():
    assert load_builtin_profiles() == len(BUILTIN_PROFILES) == 4
    assert load_builtin_profiles() == 0
    assert set(ProvisioningProfile.objects.values_list("vendor", flat=True)) == {
        "snom", "yealink", "grandstream", "cisco"}


def test_management_command_pet_provisioning_profiles():
    from django.core.management import call_command

    call_command("pet_provisioning_profiles")
    call_command("pet_provisioning_profiles")
    assert ProvisioningProfile.objects.filter(event=None).count() == 4


def test_provisioning_url_and_filename(phone, settings):
    settings.PET_PUBLIC_URL = "https://pet.example.org/"
    assert phone.mac_plain == "001122aabbcc"
    assert phone.provisioning_filename == "001122aabbcc.cfg"
    assert phone.provisioning_url == f"https://pet.example.org/prov/{phone.provisioning_token}/001122aabbcc.cfg"
    assert Device(type="sip").provisioning_url == ""


def test_prov_by_token_serves_yealink_config(client, phone):
    url = f"/prov/{phone.provisioning_token}/001122aabbcc.cfg"
    assert url == reverse("prov:by_token", args=[phone.provisioning_token, "001122aabbcc.cfg"])
    r = client.get(url)
    assert r.status_code == 200 and r["Content-Type"].startswith("text/plain")
    body = r.content.decode()
    assert f"account.1.user_name = {phone.sip_username}" in body
    assert f"account.1.auth_name = {phone.sip_username}" in body
    assert f"account.1.password = {phone.sip_password}" in body
    assert "account.1.sip_server.1.address = demo.pet.local" in body
    assert "account.1.display_name = Alice & Co" in body  # not HTML-escaped in a plain cfg
    assert "account.1.sip_server.1.transport_type = 0" in body
    phone.refresh_from_db()
    assert phone.config["last_provisioned_at"]
    # uppercase MAC in the file name is accepted too
    assert client.get(f"/prov/{phone.provisioning_token}/001122AABBCC.cfg").status_code == 200


def test_prov_by_token_404s(client, phone, profiles):
    assert client.get(f"/prov/{phone.provisioning_token}/wrongname.cfg").status_code == 404
    assert client.get("/prov/not-a-real-token-at-all-xxxxxxxx/001122aabbcc.cfg").status_code == 404
    phone.state = "disabled"
    phone.save()
    assert client.get(f"/prov/{phone.provisioning_token}/001122aabbcc.cfg").status_code == 404
    phone.state = "new"
    phone.provisioning_profile = None
    phone.save()
    assert client.get(f"/prov/{phone.provisioning_token}/001122aabbcc.cfg").status_code == 404


def test_prov_by_mac_requires_token_or_basic_auth(client, phone):
    url = "/prov/yealink/001122aabbcc.cfg"
    r = client.get(url)
    assert r.status_code == 401 and r["WWW-Authenticate"].startswith("Basic")
    assert b"requires either ?token=" in r.content
    assert client.get(url + "?token=wrong").status_code == 401
    assert client.get(url, **_basic(phone.sip_username, "wrong")).status_code == 401
    r = client.get(url + f"?token={phone.provisioning_token}")
    assert r.status_code == 200 and phone.sip_password.encode() in r.content
    r = client.get(url, **_basic(phone.sip_username, phone.sip_password))
    assert r.status_code == 200 and phone.sip_password.encode() in r.content
    # colon / dash / uppercase spellings of the MAC and .xml suffix all resolve
    assert client.get(f"/prov/yealink/00-11-22-AA-BB-CC.cfg?token={phone.provisioning_token}").status_code == 200
    assert client.get(f"/prov/yealink/00:11:22:aa:bb:cc.xml?token={phone.provisioning_token}").status_code == 200
    # wrong vendor or unknown MAC -> 404 (only once authenticated)
    assert client.get(f"/prov/snom/001122aabbcc.cfg?token={phone.provisioning_token}").status_code == 404
    assert client.get(f"/prov/yealink/ffffffffffff.cfg?token={phone.provisioning_token}").status_code == 404


def test_other_vendor_templates_render_credentials(phone, profiles):
    for vendor, needle in (("snom", "<user_pname idx=\"1\""), ("grandstream", "<P34>"), ("cisco", "<Password_1_>")):
        phone.provisioning_profile = profiles[vendor]
        body = profiles[vendor].render(phone)
        assert needle in body and phone.sip_password in body and phone.sip_username in body
        assert "demo.pet.local" in body
    assert profiles["snom"].render(phone).count("Alice &amp; Co") == 1  # XML-escaped
    assert "<P270>Alice &amp; Co</P270>" in profiles["grandstream"].render(phone)
    assert "<P35>" in profiles["grandstream"].render(phone) and "<P47>demo.pet.local:5060</P47>" in (
        profiles["grandstream"].render(phone))
    assert profiles["grandstream"].filename_pattern == "cfg{mac}.xml"
    assert profiles["cisco"].filename_pattern == "spa{mac}.cfg"


def test_device_detail_shows_provisioning_url(client, phone, event, user):
    client.force_login(user)
    r = client.get(reverse("portal:device_detail", args=[event.slug, phone.pk]))
    assert r.status_code == 200
    assert f"/prov/{phone.provisioning_token}/001122aabbcc.cfg".encode() in r.content


def test_prov_gsm_hook_route_not_shadowed(client):
    assert reverse("prov:gsm_register") == "/prov/gsm/register/"
    assert client.post("/prov/gsm/register/", {}).status_code == 401
