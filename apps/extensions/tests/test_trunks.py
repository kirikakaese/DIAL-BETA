"""SIP trunk extensions with number blocks (``4700``-``4799`` -> one remote PBX).

Covers the block helpers, block policy, conflicts in both directions (block vs. numbers, numbers vs. block,
block vs. block), registration/approval, the single-SIP-account device rule (service, portal, API), the REST
serializer, the CLI flag, the PBX route API and the portal detail page.
"""
import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.api import cli
from apps.api.tests.test_cli import FakeResponse
from apps.devices.models import Device, DeviceBinding
from apps.extensions import services
from apps.extensions.models import Extension, ExtensionType
from apps.numbering import blocks
from apps.numbering.models import NumberRange

pytestmark = pytest.mark.django_db

SECRET = "hook-secret-123"
HDR = {"HTTP_X_PET_PBX_SECRET": SECRET}


def make_trunk(event, owner, number="4700", digits=2, state=Extension.State.ACTIVE, **fields):
    return Extension.objects.create(event=event, number=number, type=ExtensionType.TRUNK, owner=owner,
                                    state=state, config={"block_digits": digits}, **fields)


def make_sip_device(event, owner, username):
    return Device.objects.create(event=event, owner=owner, type="sip", sip_username=username,
                                 sip_password="secretpw", state=Device.State.SUBSCRIBED)


# --------------------------------------------------------------------------- formal validation / helpers

@pytest.mark.parametrize("base, digits, fragment", [
    ("4711", 2, "must end in 2 zeros"),
    ("4710", 2, "must end in 2 zeros"),
    ("47", 2, "too short"),
    ("470", 3, "too short"),
    ("4700", 4, "10, 100 or 1000"),
    ("4700", 0, "10, 100 or 1000"),
    ("47a0", 1, "digits only"),
    ("", 1, "digits only"),
])
def test_validate_block_rejects(base, digits, fragment):
    assert fragment in blocks.validate_block(base, digits)


def test_validate_block_accepts_and_helpers():
    assert blocks.validate_block("4700", 2) == ""
    assert blocks.validate_block("4710", 1) == ""
    assert blocks.validate_block(" 4000 ", 3) == ""
    assert blocks.block_prefix("4700", 2) == "47"
    assert blocks.block_prefix("4700", 0) == "4700"
    assert blocks.block_last("4700", 2) == "4799"
    assert blocks.block_last("4000", 3) == "4999"


def test_model_block_helpers(event, user):
    t = make_trunk(event, user, "4700", 2)
    assert t.is_trunk and t.accepts_devices and not t.is_endpoint
    assert t.block_digits == 2 and t.block_size == 100
    assert t.block_prefix == "47" and t.block_pattern == "_47XX"
    assert t.block_range() == ("4700", "4799")
    assert t.number_label == "4700–4799"
    assert t.covers("4700") and t.covers("4711") and t.covers("4799")
    assert not t.covers("4800") and not t.covers("471") and not t.covers("47111") and not t.covers("47ab")
    # non-trunks have no block
    plain = Extension.objects.create(event=event, number="4242", type=ExtensionType.SIP, owner=user,
                                     state=Extension.State.ACTIVE, config={"block_digits": 2})
    assert plain.block_digits == 0 and plain.number_label == "4242" and not plain.covers("4242")
    # broken config -> no block
    t.config = {"block_digits": "many"}
    assert t.block_digits == 0 and t.number_label == "4700"


# --------------------------------------------------------------------------- policy

def test_evaluate_block_format_error(event, user):
    res = blocks.evaluate_block(services.get_plan(event), "4711", 2, user=user)
    assert not res.allowed and res.code == "format"


def test_evaluate_block_restricted_range_denied_for_plain_user(event, user, orga):
    plan = services.get_plan(event)
    res = blocks.evaluate_block(plan, "1000", 3, user=user)
    assert not res.allowed and res.code == "restricted"
    res = blocks.evaluate_block(plan, "1000", 3, user=orga)
    assert res.allowed and res.requires_approval and res.range.name == "Orga"


def test_evaluate_block_allowed_always_requires_approval(event, user):
    plan = services.get_plan(event)
    assert not plan.evaluate("4700", user=user).requires_approval
    res = blocks.evaluate_block(plan, "4700", 2, user=user)
    assert res.allowed and res.requires_approval and res.range is None


def test_evaluate_block_containing_service_number(event, user):
    plan = services.get_plan(event)
    plan.dect_claim_number = "4750"
    plan.save()
    res = blocks.evaluate_block(plan, "4700", 2, user=user)
    assert not res.allowed and res.code == "service" and "4750" in res.reason
    # a smaller block that does not contain it is fine
    assert blocks.evaluate_block(plan, "4710", 1, user=user).allowed


def test_evaluate_block_containing_emergency_number(event, user):
    plan = services.get_plan(event)
    plan.emergency_numbers = ["112", "4733"]
    plan.save()
    res = blocks.evaluate_block(plan, "4700", 2, user=user)
    assert not res.allowed and res.code == "emergency" and "4733" in res.reason


def test_evaluate_block_blocked_or_restricted_subrange(event, user, orga):
    plan = services.get_plan(event)
    NumberRange.objects.create(plan=plan, name="Reserved 475x", prefix="475", mode="blocked", priority=3)
    res = blocks.evaluate_block(plan, "4700", 2, user=user)
    assert not res.allowed and res.code == "blocked"
    # the first/last numbers of the block are checked directly
    res = blocks.evaluate_block(plan, "0000", 3, user=orga)
    assert not res.allowed and res.code == "blocked"
    res = blocks.evaluate_block(plan, "9900", 2, user=orga)
    assert not res.allowed and res.code in ("blocked", "service")


# --------------------------------------------------------------------------- conflicts

def test_existing_number_blocks_trunk(event, user, other_user):
    services.register(event, other_user, "4711", ExtensionType.DECT)
    av = services.check_availability(event, "4700", user=user, extension_type=ExtensionType.TRUNK, block_digits=2)
    assert av.taken and not av.available
    assert av.conflicts == ["4711"] and av.block_digits == 2 and av.suggestions == []
    assert "overlaps existing numbers: 4711" in av.reason
    # a block that does not contain the number is fine
    av = services.check_availability(event, "4720", user=user, extension_type=ExtensionType.TRUNK, block_digits=1)
    assert not av.taken and av.available and av.requires_approval
    with pytest.raises(services.ExtensionError, match="4711"):
        services.register(event, user, "4700", ExtensionType.TRUNK, config={"block_digits": 2})


def test_active_trunk_blocks_numbers_and_overlapping_trunks(event, user, orga):
    trunk = services.register(event, orga, "4700", ExtensionType.TRUNK, config={"block_digits": 2})
    assert trunk.state == Extension.State.ACTIVE
    # a plain number inside the block
    av = services.check_availability(event, "4711", user=user)
    assert av.taken and av.blocks == ["4700–4799"] and av.conflicts == []
    assert "inside the trunk block 4700–4799" in av.reason
    with pytest.raises(services.ExtensionError, match="4700–4799"):
        services.register(event, user, "4711", ExtensionType.DECT)
    # the base itself
    assert services.check_availability(event, "4700", user=user).taken
    # an overlapping smaller block based at the same number
    av = services.check_availability(event, "4700", user=user, extension_type=ExtensionType.TRUNK, block_digits=1)
    assert av.taken and av.blocks == ["4700–4799"]
    # an overlapping bigger block
    av = services.check_availability(event, "4000", user=orga, extension_type=ExtensionType.TRUNK, block_digits=3)
    assert av.taken and av.blocks == ["4700–4799"]
    # a disjoint block is fine
    av = services.check_availability(event, "4800", user=user, extension_type=ExtensionType.TRUNK, block_digits=2)
    assert not av.taken
    assert services.trunk_for_number(event, "4711") == trunk
    assert services.trunk_for_number(event, "4800") is None
    assert services.overlapping_trunks(event, "4750") == [trunk]
    assert services.overlapping_trunks(event, "4750", exclude=trunk) == []


def test_requested_trunk_occupies_block_but_deleted_does_not(event, user, other_user):
    trunk = services.register(event, user, "4700", ExtensionType.TRUNK, config={"block_digits": 2})
    assert trunk.state == Extension.State.REQUESTED
    assert services.check_availability(event, "4711", user=other_user).taken
    assert services.trunk_for_number(event, "4711") is None  # only *active* trunks route
    services.delete(trunk, user)
    assert not services.check_availability(event, "4711", user=other_user).taken


def test_blockers_index_honours_trunk_blocks(event, user, orga):
    from apps.numbering.services import Blockers

    services.register(event, orga, "4700", ExtensionType.TRUNK, config={"block_digits": 2})
    b = Blockers(event, services.get_plan(event), user=user)
    assert b.blocks("4700") and b.blocks("4711") and b.blocks("4799")
    assert b.blocks("47")  # prefix-free: the block prefix itself is occupied
    assert not b.blocks("4800") and not b.blocks("4242")


def test_availability_api_with_block(event, user, other_user):
    c = APIClient()
    c.force_authenticate(user)
    d = c.get("/api/v1/availability/?event=demo&number=4700&type=trunk&block_digits=2").json()
    assert d["available"] is True and d["taken"] is False and d["requires_approval"] is True
    assert d["block_digits"] == 2 and d["blocks"] == []
    services.register(event, other_user, "4711", ExtensionType.DECT)
    d = c.get("/api/v1/availability/?event=demo&number=4700&type=trunk&block_digits=2").json()
    assert d["available"] is False and d["taken"] is True and d["conflicts"] == ["4711"]
    assert "4711" in d["reason"]
    # block_digits is ignored for other types
    d = c.get("/api/v1/availability/?event=demo&number=4700&type=sip&block_digits=2").json()
    assert d["block_digits"] == 0 and d["available"] is True


# --------------------------------------------------------------------------- registration

def test_register_requires_block_digits(event, user):
    with pytest.raises(services.ExtensionError, match="block size"):
        services.register(event, user, "4700", ExtensionType.TRUNK)
    with pytest.raises(services.ExtensionError, match="10, 100 or 1000"):
        services.register(event, user, "4700", ExtensionType.TRUNK, config={"block_digits": 5})
    with pytest.raises(services.ExtensionError, match="zeros"):
        services.register(event, user, "4711", ExtensionType.TRUNK, config={"block_digits": 2})
    assert not Extension.objects.filter(event=event).exists()


def test_register_trunk_user_needs_approval_orga_is_active(event, user, orga):
    ext = services.register(event, user, "4700", ExtensionType.TRUNK, config={"block_digits": 2})
    assert ext.state == Extension.State.REQUESTED and ext.block_digits == 2
    services.approve(ext, orga)
    ext.refresh_from_db()
    assert ext.state == Extension.State.ACTIVE
    ext2 = services.register(event, orga, "4800", ExtensionType.TRUNK, force_active=True,
                             config={"block_digits": 2, "note": "keep"})
    assert ext2.state == Extension.State.ACTIVE and ext2.config == {"block_digits": 2, "note": "keep"}
    ext3 = services.register(event, orga, "4900", ExtensionType.TRUNK, config={"block_digits": 1})
    assert ext3.state == Extension.State.ACTIVE  # orga never needs approval


# --------------------------------------------------------------------------- device binding rule

def test_validate_device_binding(event, user):
    trunk = make_trunk(event, user)
    plain = Extension.objects.create(event=event, number="4242", type=ExtensionType.DECT, owner=user,
                                     state=Extension.State.ACTIVE)
    services.validate_device_binding(plain, "dect")  # no rule for endpoints
    services.validate_device_binding(trunk, "sip")
    for bad in ("dect", "gsm", "webrtc", "analog"):
        with pytest.raises(services.ExtensionError, match="only be bound to a SIP account"):
            services.validate_device_binding(trunk, bad)
    dev = make_sip_device(event, user, "demo-pbx")
    DeviceBinding.objects.create(extension=trunk, device=dev)
    with pytest.raises(services.ExtensionError, match="exactly one SIP account"):
        services.validate_device_binding(trunk, "sip")
    services.validate_device_binding(trunk, "sip", device=dev)  # re-binding the same account is fine


def test_portal_device_add_for_trunk(client, event, user, member):
    trunk = make_trunk(event, user)
    url = reverse("portal:device_add", args=[event.slug, trunk.pk])
    client.force_login(user)
    r = client.get(url)
    assert r.status_code == 200
    # a DECT handset is not a valid choice for a trunk
    r = client.post(url, {"endpoint_type": "dect", "ipei": "0123456789012", "name": "orange"})
    assert r.status_code == 200 and not trunk.bindings.exists()
    # one SIP account is fine
    r = client.post(url, {"endpoint_type": "sip", "name": "village-pbx", "sip_transport": "udp", "mac_address": ""})
    assert r.status_code == 302, r.content
    dev = trunk.bindings.get().device
    assert dev.type == "sip" and dev.sip_username and dev.sip_password
    # a second one is refused
    r = client.post(url, {"endpoint_type": "sip", "name": "second", "sip_transport": "udp", "mac_address": ""})
    assert r.status_code == 302 and r.url == reverse("portal:extension_detail", args=[event.slug, trunk.pk])
    assert trunk.bindings.count() == 1


def test_api_bind_device_enforces_trunk_rule(event, user, member):
    trunk = make_trunk(event, user)
    dect = Device.objects.create(event=event, owner=user, type="dect", ipei="0123456789012")
    sip1 = make_sip_device(event, user, "demo-pbx1")
    sip2 = make_sip_device(event, user, "demo-pbx2")
    c = APIClient()
    c.force_authenticate(user)
    url = f"/api/v1/extensions/{trunk.pk}/bind-device/"
    r = c.post(url, {"device": str(dect.pk)}, format="json")
    assert r.status_code == 400 and "SIP account" in r.json()["detail"]
    r = c.post(url, {"device": str(sip1.pk)}, format="json")
    assert r.status_code == 200, r.content
    assert [d["sip_username"] for d in r.json()["devices"]] == ["demo-pbx1"]
    r = c.post(url, {"device": str(sip2.pk)}, format="json")
    assert r.status_code == 400 and "exactly one" in r.json()["detail"]
    assert trunk.bindings.count() == 1
    # devices created via the API against a trunk follow the same rule
    r = c.post("/api/v1/devices/", {"event": "demo", "type": "dect", "extension": str(trunk.pk),
                                    "ipei": "0123456789013"}, format="json")
    assert r.status_code == 400


# --------------------------------------------------------------------------- REST serializer / CLI

def test_api_create_trunk(event, user, orga):
    c = APIClient()
    c.force_authenticate(orga)
    r = c.post("/api/v1/extensions/", {"event": "demo", "number": "4700", "type": "trunk", "block_digits": 2,
                                       "display_name": "Village PBX"}, format="json")
    assert r.status_code == 201, r.content
    d = r.json()
    assert d["state"] == "active" and d["type"] == "trunk"
    assert d["block_digits"] == 2 and d["block_range"] == ["4700", "4799"]
    assert d["config"]["block_digits"] == 2
    ext = Extension.objects.get(pk=d["id"])
    assert ext.block_digits == 2 and ext.owner == orga
    # block_digits cannot be changed afterwards
    r = c.patch(f"/api/v1/extensions/{ext.pk}/", {"block_digits": 1, "display_name": "x"}, format="json")
    assert r.status_code == 200 and r.json()["block_digits"] == 2 and r.json()["display_name"] == "x"
    # missing / invalid block size
    r = c.post("/api/v1/extensions/", {"event": "demo", "number": "4800", "type": "trunk"}, format="json")
    assert r.status_code == 400 and "block size" in r.json()["detail"]
    r = c.post("/api/v1/extensions/", {"event": "demo", "number": "4800", "type": "trunk", "block_digits": 4},
               format="json")
    assert r.status_code == 400
    # a plain user gets a pending request; non-trunks report no block
    c.force_authenticate(user)
    r = c.post("/api/v1/extensions/", {"event": "demo", "number": "4800", "type": "trunk", "block_digits": 1},
               format="json")
    assert r.status_code == 201 and r.json()["state"] == "requested" and r.json()["block_range"] == ["4800", "4809"]
    r = c.post("/api/v1/extensions/", {"event": "demo", "number": "4242", "type": "sip"}, format="json")
    assert r.status_code == 201 and r.json()["block_digits"] is None and r.json()["block_range"] is None


def test_cli_block_digits(capsys):
    args = cli.build_parser().parse_args(["extensions", "create", "--event", "demo", "--number", "4700",
                                          "--type", "trunk", "--block-digits", "2"])
    assert args.block_digits == 2
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["extensions", "create", "--event", "demo", "--number", "4700",
                                       "--type", "trunk", "--block-digits", "4"])
    resp = FakeResponse({"id": "t1", "number": "4700", "type": "trunk", "state": "requested",
                         "block_digits": 2, "block_range": ["4700", "4799"]})
    from unittest import mock

    with mock.patch("requests.Session.request", return_value=resp) as req:
        rc = cli.main(["--url", "http://pet.test", "extensions", "create", "--event", "demo", "--number", "4700",
                       "--type", "trunk", "--block-digits", "2"])
    assert rc == 0
    assert req.call_args.args == ("POST", "http://pet.test/api/v1/extensions/")
    assert req.call_args.kwargs["json"] == {"event": "demo", "number": "4700", "type": "trunk", "block_digits": 2}
    assert "4700-4799 (trunk) -> requested" in capsys.readouterr().out
    # without the flag the payload has no block
    with mock.patch("requests.Session.request", return_value=FakeResponse(
            {"id": "x", "number": "4242", "type": "sip", "state": "active"})) as req:
        cli.main(["--url", "http://pet.test", "extensions", "create", "--event", "demo", "--number", "4242",
                  "--type", "sip"])
    assert "block_digits" not in req.call_args.kwargs["json"]


# --------------------------------------------------------------------------- PBX route API

def test_route_api_for_number_inside_block(event, user, settings):
    settings.PET_PBX_HOOK_SECRET = SECRET
    trunk = make_trunk(event, user, "4700", 2, ring_timeout=25, priority=2)
    c = APIClient()
    # without a SIP account nothing can be dialled
    d = c.get("/api/v1/pbx/route/?event=demo&number=4711", **HDR).json()
    assert d["type"] == "trunk" and d["targets"] == [] and d["trunk"] == {"base": "4700", "range": ["4700", "4799"]}
    DeviceBinding.objects.create(extension=trunk, device=make_sip_device(event, user, "demo-pbx"))
    d = c.get("/api/v1/pbx/route/?event=demo&number=4711", **HDR).json()
    assert d["type"] == "trunk" and d["targets"] == ["PJSIP/4711@demo-pbx"]
    assert d["dial_string"] == "PJSIP/4711@demo-pbx" and d["timeout"] == 25 and d["priority"] == 2
    assert d["trunk"] == {"base": "4700", "range": ["4700", "4799"]}
    # the base number is an extension of its own and routes the same way
    d = c.get("/api/v1/pbx/route/?event=demo&number=4700", **HDR).json()
    assert d["type"] == "trunk" and d["targets"] == ["PJSIP/4700@demo-pbx"]
    # outside the block: unknown
    d = c.get("/api/v1/pbx/route/?event=demo&number=4800", **HDR).json()
    assert d["type"] is None and d["targets"] == []
    # a suspended trunk does not route
    trunk.state = Extension.State.SUSPENDED
    trunk.save()
    d = c.get("/api/v1/pbx/route/?event=demo&number=4711", **HDR).json()
    assert d["type"] is None


# --------------------------------------------------------------------------- portal pages

def test_extension_detail_renders_trunk_instructions(client, event, user, member):
    trunk = make_trunk(event, user, display_name="Village PBX")
    client.force_login(user)
    url = reverse("portal:extension_detail", args=[event.slug, trunk.pk])
    html = client.get(url).content.decode()
    assert "4700–4799" in html and "How to connect your PBX" in html
    assert "<strong>4700</strong> to <strong>4799</strong>" in html
    assert "Create SIP account" in html and "100 numbers" in html
    assert "caller ID 4700" in html
    dev = make_sip_device(event, user, "demo-pbx")
    DeviceBinding.objects.create(extension=trunk, device=dev)
    html = client.get(url).content.decode()
    assert "demo-pbx@demo.pet.local" in html and "Create SIP account" not in html
    # normal extensions are unchanged
    plain = services.register(event, user, "4242", ExtensionType.SIP)
    html = client.get(reverse("portal:extension_detail", args=[event.slug, plain.pk])).content.decode()
    assert "How to connect your PBX" not in html and "Number block" not in html
    assert "How to connect a softphone" in html and "Add device" in html


def test_extension_create_form_trunk(client, event, user, member):
    client.force_login(user)
    url = reverse("portal:extension_create", args=[event.slug])
    r = client.get(url)
    assert r.status_code == 200 and b'name="block_digits"' in r.content
    # base not ending in zeros -> error, nothing created
    r = client.post(url, {"number": "4711", "type": "trunk", "block_digits": "2", "in_phonebook": "on"})
    assert r.status_code == 200 and b"zeros" in r.content
    assert not Extension.objects.filter(event=event).exists()
    r = client.post(url, {"number": "4700", "type": "trunk", "block_digits": "2", "in_phonebook": "on"})
    ext = Extension.objects.get(event=event, number="4700")
    assert r.status_code == 302 and ext.state == Extension.State.REQUESTED and ext.block_digits == 2
    # block_digits is ignored for other types
    client.post(url, {"number": "4242", "type": "sip", "block_digits": "2"})
    assert Extension.objects.get(event=event, number="4242").config == {}
