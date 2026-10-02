"""Provisioning: extension → devices → OMM subscriptions via the dummy backend."""
import pytest

from apps.core.models import AuditLog
from apps.dect import provisioning
from apps.dect.base import DECTError
from apps.devices.models import Device, DeviceBinding
from apps.extensions import services as ext_services

pytestmark = pytest.mark.django_db


def test_provision_creates_subscription_and_stores_ppn(dect, dect_extension, dect_device):
    done = provisioning.provision_dect_for_extension(dect_extension)
    assert len(done) == 1
    dect_device.refresh_from_db()
    assert dect_device.omm_ppn == "1" and dect_device.omm_user_id == "1"
    assert dect_device.state == Device.State.PENDING
    assert dect_device.sip_username and dect_device.sip_password and dect_device.subscription_pin
    sub = dect.subs["1"]
    assert sub["ipei"] == "0123456789012" and sub["number"] == "4242" and sub["pin"] == dect_device.subscription_pin
    assert dect.subscription_window_minutes == 30
    assert dect_device.config["provisioned_pin"] == dect_device.subscription_pin
    assert AuditLog.objects.filter(action="provision").exists()


def test_provision_twice_updates_instead_of_creating(dect, dect_extension, dect_device):
    provisioning.provision_dect_for_extension(dect_extension)
    dect_extension.display_name = "Alice B"
    dect_extension.save()
    provisioning.provision_dect_for_extension(dect_extension)
    assert len(dect.subs) == 1 and dect.subs["1"]["display_name"] == "Alice B"


def test_new_pin_recreates_subscription_when_not_yet_subscribed(dect, dect_extension, dect_device):
    provisioning.provision_dect_for_extension(dect_extension)
    dect_device.refresh_from_db()
    old_pin = dect_device.subscription_pin
    dect_device.issue_subscription_pin()
    assert dect_device.subscription_pin != old_pin
    provisioning.provision_device(dect_device)
    dect_device.refresh_from_db()
    assert dect_device.omm_ppn == "2" and "1" not in dect.subs and dect.subs["2"]["pin"] == dect_device.subscription_pin


def test_provision_error_recorded_and_reraised(dect, dect_extension, dect_device, monkeypatch):
    def boom(**kw):
        raise DECTError("OMM says no")

    monkeypatch.setattr(dect, "create_subscription", boom)
    with pytest.raises(DECTError, match="OMM says no"):
        provisioning.provision_dect_for_extension(dect_extension)
    dect_device.refresh_from_db()
    assert "OMM says no" in dect_device.config["provision_error"]
    assert dect_device.omm_ppn == ""


def test_deprovision_deletes_only_when_last_binding(dect, event, user, member, dect_extension, dect_device):
    provisioning.provision_dect_for_extension(dect_extension)
    other = ext_services.register(event, user, "4343", "dect")
    DeviceBinding.objects.create(extension=other, device=dect_device, priority=5)
    provisioning.deprovision_dect_for_extension(dect_extension)
    # still bound to 4343 → subscription re-pointed, not deleted
    assert dect.subs["1"]["number"] == "4343"
    dect_device.refresh_from_db()
    assert dect_device.omm_ppn == "1"
    DeviceBinding.objects.filter(extension=dect_extension).delete()
    provisioning.deprovision_dect_for_extension(other)
    dect_device.refresh_from_db()
    assert dect.subs == {} and dect_device.omm_ppn == "" and dect_device.state == Device.State.NEW


def test_provision_device_ignores_non_dect(dect, event, user):
    sip = Device.objects.create(event=event, owner=user, type="sip")
    assert provisioning.provision_device(sip) is None


def test_provision_device_without_binding_raises(dect, event, user):
    d = Device.objects.create(event=event, owner=user, type="dect", ipei="0123456789013")
    with pytest.raises(DECTError):
        provisioning.provision_device(d)
