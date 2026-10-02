"""Feature #19: renaming an active DECT extension updates the handset's name on the OMM in place.

A pure ``display_name`` change must reach every bound handset as ``SetPPUser`` (``update_subscription``)
and must never delete/recreate the subscription - the handset stays subscribed, PIN and PPN unchanged.
"""
import pytest

from apps.dect import provisioning
from apps.dect.backends.omm import MitelOMM
from apps.dect.tests.test_omm import make_client
from apps.devices.models import Device
from apps.extensions import services as ext_services
from apps.extensions.models import Extension
from apps.pbx.tests.conftest import bind, make_device, make_extension

pytestmark = pytest.mark.django_db


def _handset(event, owner, ext, ipei, *, priority=0):
    """A DECT handset bound to ``ext`` that has already completed its subscription on the OMM."""
    device = make_device(event, f"demo-{ipei[-4:]}", dtype="dect", owner=owner, name=f"hs-{ipei[-4:]}")
    device.ipei = ipei
    device.state = Device.State.NEW
    device.save()
    bind(ext, device, priority=priority)
    return device


def _subscribe_all(dect, ext):
    provisioning.provision_dect_for_extension(ext)
    devices = list(Device.objects.filter(bindings__extension=ext).distinct())
    for d in devices:
        assert d.omm_ppn and d.subscription_pin and d.config["provisioned_pin"] == d.subscription_pin
        d.state = Device.State.SUBSCRIBED  # what sync_infrastructure() records once the handset registered
        d.save(update_fields=["state"])
    dect.calls.clear()
    return devices


def _snapshot(device):
    return {
        "omm_ppn": device.omm_ppn, "omm_user_id": device.omm_user_id, "subscription_pin": device.subscription_pin,
        "subscription_pin_expires_at": device.subscription_pin_expires_at, "state": device.state,
        "provisioned_pin": device.config.get("provisioned_pin"),
    }


@pytest.fixture
def alice_ext(event, user, member):
    return make_extension(event, "4242", etype="dect", owner=user, display_name="Alice")


def test_rename_updates_handset_in_place(dect, event, user, alice_ext):
    device = _handset(event, user, alice_ext, "0123456789012")
    _subscribe_all(dect, alice_ext)
    before = _snapshot(Device.objects.get(pk=device.pk))
    ppn = before["omm_ppn"]

    ext_services.update(alice_ext, user, display_name="Alice Wonderland")  # eager celery → provisioning

    methods = [m for m, _ in dect.calls]
    assert "update_subscription" in methods
    assert "create_subscription" not in methods and "delete_subscription" not in methods
    upd = [kw for m, kw in dect.calls if m == "update_subscription"]
    assert upd == [{"ppn": ppn, "number": "4242", "display_name": "Alice Wonderland", "encryption": False}]
    assert dect.subs[ppn]["display_name"] == "Alice Wonderland"
    assert dect.subs[ppn]["pin"] == before["subscription_pin"]  # AC on the OMM untouched

    device.refresh_from_db()
    assert _snapshot(device) == before
    assert device.state == Device.State.SUBSCRIBED
    alice_ext.refresh_from_db()
    assert alice_ext.provision_error == ""


def test_rename_reaches_every_bound_handset(dect, event, user, alice_ext):
    d1 = _handset(event, user, alice_ext, "0123456789012", priority=0)
    d2 = _handset(event, user, alice_ext, "0123456789013", priority=1)
    _subscribe_all(dect, alice_ext)
    d1.refresh_from_db()
    d2.refresh_from_db()

    ext_services.update(alice_ext, user, display_name="Infodesk")

    upd = {kw["ppn"]: kw["display_name"] for m, kw in dect.calls if m == "update_subscription"}
    assert upd == {d1.omm_ppn: "Infodesk", d2.omm_ppn: "Infodesk"}
    assert [m for m, _ in dect.calls if m != "update_subscription"] == []
    assert len(dect.subs) == 2
    for d in (d1, d2):
        snap = _snapshot(d)
        d.refresh_from_db()
        assert _snapshot(d) == snap and d.state == Device.State.SUBSCRIBED


def test_rename_of_pending_handset_does_not_recreate(dect, event, user, alice_ext):
    """Handset created on the OMM but not yet registered: same PIN → plain update, no new PIN."""
    device = _handset(event, user, alice_ext, "0123456789012")
    provisioning.provision_dect_for_extension(alice_ext)
    dect.calls.clear()
    device.refresh_from_db()
    assert device.state == Device.State.PENDING
    before = _snapshot(device)

    ext_services.update(alice_ext, user, display_name="Alice W.")

    assert [m for m, _ in dect.calls] == ["update_subscription"]
    device.refresh_from_db()
    assert _snapshot(device) == before


def test_name_fallbacks_reach_the_omm(dect, event, user, alice_ext):
    device = _handset(event, user, alice_ext, "0123456789012")
    _subscribe_all(dect, alice_ext)
    device.refresh_from_db()
    # empty display name → owner's username is what the handset shows
    ext_services.update(alice_ext, user, display_name="")
    assert dect.subs[device.omm_ppn]["display_name"] == "alice"
    assert alice_ext.caller_id_name == "alice"


def test_display_mode_does_not_change_handset_name(dect, event, user, alice_ext):
    """``display_mode`` shapes the *caller ID* sent to called parties; the handset's own OMM name stays the name."""
    device = _handset(event, user, alice_ext, "0123456789012")
    _subscribe_all(dect, alice_ext)
    device.refresh_from_db()
    ext_services.update(alice_ext, user, display_mode=Extension.DisplayMode.NUMBER)
    assert alice_ext.caller_id_display == "4242" and alice_ext.caller_id_name == "Alice"
    assert dect.subs[device.omm_ppn]["display_name"] == "Alice"
    assert [m for m, _ in dect.calls] == ["update_subscription"]


def test_omm_truncates_name_to_20_chars():
    client, sock = make_client({
        "GetPPDev": lambda e: f'<GetPPDevResp><pp ppn="{e.get("startPPN")}" uid="9"/></GetPPDevResp>',
        "SetPPUser": lambda e: "<SetPPUserResp/>",
    })
    assert isinstance(client, MitelOMM)
    long_name = "Alice Wonderland from the Hackcenter"
    client.update_subscription(ppn="5", number="4242", display_name=long_name)
    upd = next(e for e in sock.sent if e.tag == "SetPPUser").find("user")
    assert upd.get("name") == long_name[:20] and len(upd.get("name")) == 20
    assert [e.tag for e in sock.sent if e.tag.startswith(("Create", "Delete"))] == []


def test_rename_secondary_extension_keeps_primary_identity(dect, event, user, alice_ext):
    """A handset bound to two extensions keeps its primary (lowest priority) number on the OMM."""
    helpdesk = make_extension(event, "4500", etype="dect", owner=user, display_name="Helpdesk")
    device = _handset(event, user, alice_ext, "0123456789012", priority=0)
    bind(helpdesk, device, priority=5)
    _subscribe_all(dect, alice_ext)
    device.refresh_from_db()
    ppn = device.omm_ppn

    ext_services.update(helpdesk, user, display_name="Helpdesk Night")

    assert dect.subs[ppn]["number"] == "4242" and dect.subs[ppn]["display_name"] == "Alice"
    assert "create_subscription" not in [m for m, _ in dect.calls]
    assert "delete_subscription" not in [m for m, _ in dect.calls]
    device.refresh_from_db()
    assert device.omm_ppn == ppn and device.state == Device.State.SUBSCRIBED
