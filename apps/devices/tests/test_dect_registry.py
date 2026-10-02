"""DECT handset registry: vendor lookup, handset history, reuse across events, orga vendor table."""
import datetime as dt

import pytest
from django.urls import reverse

from apps.devices import services
from apps.devices.dect_vendors import BUILTIN_MANUFACTURERS, load_builtin_manufacturers
from apps.devices.models import DECTManufacturer, Device, DeviceBinding
from apps.events.models import Event, EventMembership
from apps.extensions.models import Extension

pytestmark = pytest.mark.django_db

IPEI = "1011400123457"  # EMC 10114


@pytest.fixture
def vendor(db):
    return DECTManufacturer.objects.create(emc="10114", name="Mitel", source="builtin")


@pytest.fixture
def old_event(db):
    today = dt.date.today()
    return Event.objects.create(name="Last Camp", slug="last", state=Event.State.ARCHIVED,
                                start_date=today - dt.timedelta(days=400), end_date=today - dt.timedelta(days=395))


def test_vendor_for_ipei(vendor):
    assert services.emc_of(IPEI) == "10114"
    assert services.vendor_for_ipei(IPEI) == vendor
    assert services.vendor_for_ipei("10114 0012345 7") == vendor  # tolerant to spaces
    assert services.vendor_for_ipei("9999900000000") is None
    assert services.vendor_for_ipei("") is None and services.vendor_for_ipei("123") is None


def test_device_manufacturer_property(event, user, vendor):
    d = Device.objects.create(event=event, owner=user, type="dect", ipei=IPEI)
    assert d.emc == "10114" and d.manufacturer == vendor
    assert Device(ipei="").manufacturer is None and Device(ipei="").emc == ""


def test_load_builtin_manufacturers_is_idempotent_and_keeps_user_rows():
    created, _ = load_builtin_manufacturers()
    assert created == len(BUILTIN_MANUFACTURERS) > 0
    assert set(DECTManufacturer.objects.values_list("source", flat=True)) == {"builtin"}
    emc = next(iter(BUILTIN_MANUFACTURERS))
    DECTManufacturer.objects.filter(emc=emc).update(name="Renamed by user", source="user")
    created, updated = load_builtin_manufacturers(overwrite_names=True)
    assert (created, updated) == (0, 0)
    assert DECTManufacturer.objects.get(emc=emc).name == "Renamed by user"


def test_management_command_pet_dect_vendors():
    from django.core.management import call_command

    call_command("pet_dect_vendors")
    call_command("pet_dect_vendors")
    assert DECTManufacturer.objects.count() == len(BUILTIN_MANUFACTURERS)


def test_suggest_vendor_never_overwrites(vendor):
    m = services.suggest_vendor("10114", "Somebody else")
    assert m == vendor and m.name == "Mitel"
    m = services.suggest_vendor("00001", "Acme Handsets", models_hint="A1")
    assert m.source == "user" and m.name == "Acme Handsets" and m.models_hint == "A1"
    with pytest.raises(services.DeviceServiceError):
        services.suggest_vendor("123", "x")
    with pytest.raises(services.DeviceServiceError):
        services.suggest_vendor("12345", "  ")


def test_handset_history_groups_by_ipei(event, old_event, user, member, vendor):
    old = Device.objects.create(event=old_event, owner=user, type="dect", ipei=IPEI, handset_model="612d",
                                name="orange", uak="deadbeef")
    EventMembership.objects.create(event=old_event, user=user)
    old_ext = Extension.objects.create(event=old_event, owner=user, number="4711", type="dect", state="active")
    DeviceBinding.objects.create(extension=old_ext, device=old)
    Device.objects.create(event=old_event, owner=user, type="dect", ipei="0032900000001")
    Device.objects.create(event=event, owner=user, type="sip")  # not DECT -> ignored
    hist = services.handset_history(user)
    assert [h.ipei for h in hist] == [IPEI, "0032900000001"]
    h = hist[0]
    assert h.vendor == vendor and h.model == "612d" and h.name == "orange"
    assert len(h.uses) == 1 and h.uses[0].event == old_event and h.uses[0].numbers == ["4711"]
    assert h.device_in(event) is None and h.latest == old
    assert hist[1].vendor is None


def test_reuse_creates_copy_then_redirects_to_existing(client, event, old_event, user, member):
    old = Device.objects.create(event=old_event, owner=user, type="dect", ipei=IPEI, handset_model="612d",
                                name="orange", uak="deadbeef", state="subscribed", omm_ppn="42")
    client.force_login(user)
    r = client.get(reverse("devices:history", args=[event.slug]))
    assert r.status_code == 200 and IPEI.encode() in r.content and b"Reuse in this event" in r.content

    r = client.post(reverse("devices:reuse", args=[event.slug, old.pk]))
    new = Device.objects.get(event=event, ipei=IPEI)
    assert r.status_code == 302 and r.url == reverse("portal:device_detail", args=[event.slug, new.pk])
    assert new.pk != old.pk and new.owner == user and new.state == "new"
    assert (new.handset_model, new.name, new.uak) == ("612d", "orange", "deadbeef")
    assert new.omm_ppn == "" and new.sip_username == ""  # nothing OMM/SIP specific is copied

    # second time: IPEI already in this event -> redirect to the existing device, no duplicate
    r = client.post(reverse("devices:reuse", args=[event.slug, old.pk]), follow=True)
    assert Device.objects.filter(event=event, ipei=IPEI).count() == 1
    assert b"already registered" in r.content
    # the history page now shows it as present
    r = client.get(reverse("devices:history", args=[event.slug]))
    assert b"In this event" in r.content


def test_reuse_requires_ownership(client, event, old_event, user, other_user, member):
    old = Device.objects.create(event=old_event, owner=other_user, type="dect", ipei=IPEI)
    client.force_login(user)
    assert client.post(reverse("devices:reuse", args=[event.slug, old.pk])).status_code == 403
    assert not Device.objects.filter(event=event, ipei=IPEI).exists()


def test_suggest_vendor_view_from_device_detail(client, event, user, member):
    d = Device.objects.create(event=event, owner=user, type="dect", ipei="0000100000000")
    client.force_login(user)
    r = client.get(reverse("portal:device_detail", args=[event.slug, d.pk]))
    assert r.status_code == 200 and b"EMC 00001" in r.content
    r = client.post(reverse("devices:suggest_vendor", args=[event.slug, d.pk]), {"name": "Acme"})
    assert r.status_code == 302
    m = DECTManufacturer.objects.get(emc="00001")
    assert m.name == "Acme" and m.source == "user"
    r = client.get(reverse("portal:device_detail", args=[event.slug, d.pk]))
    assert b"Acme" in r.content and b"user contributed" in r.content


def test_manufacturers_page_orga_only_with_counts(client, event, user, orga, member, vendor):
    Device.objects.create(event=event, owner=user, type="dect", ipei=IPEI)
    Device.objects.create(event=event, owner=user, type="dect", ipei="1011400000002")
    Device.objects.create(event=event, owner=user, type="dect", ipei="7777700000002")
    url = reverse("devices:manufacturers", args=[event.slug])
    client.force_login(user)
    assert client.get(url).status_code == 403
    client.force_login(orga)
    r = client.get(url)
    assert r.status_code == 200
    rows = {r_["emc"]: r_ for r_ in services.manufacturer_stats(event)}
    assert rows["10114"]["count"] == 2 and rows["10114"]["manufacturer"] == vendor
    assert rows["77777"]["count"] == 1 and rows["77777"]["manufacturer"] is None
    # add + rename via the form
    r = client.post(url, {"emc": "77777", "name": "Foo Phones", "models_hint": "F1"})
    assert r.status_code == 302 and DECTManufacturer.objects.get(emc="77777").name == "Foo Phones"
    client.post(url, {"emc": "77777", "name": "Foo Telecom", "models_hint": ""})
    m = DECTManufacturer.objects.get(emc="77777")
    assert m.name == "Foo Telecom" and m.models_hint == "F1"
    r = client.post(url, {"emc": "12", "name": "bad"})
    assert r.status_code == 200 and b"EMC" in r.content


def test_portal_device_detail_still_resolves_next_to_devices_mount(client, event, user, member):
    d = Device.objects.create(event=event, owner=user, type="dect", ipei=IPEI)
    url = reverse("portal:device_detail", args=[event.slug, d.pk])
    assert url == f"/e/{event.slug}/devices/{d.pk}/"
    client.force_login(user)
    assert client.get(url).status_code == 200
    assert reverse("devices:history", args=[event.slug]) == f"/e/{event.slug}/devices/history/"
    assert client.get(reverse("devices:history", args=[event.slug])).status_code == 200
