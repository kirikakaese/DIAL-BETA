"""Shared fixtures for messaging tests: a DECT handset bound to alice's extension and subscribed on the dummy OMM."""
import pytest

from apps.dect import get_dect
from apps.devices.models import Device, DeviceBinding
from apps.extensions.services import register


@pytest.fixture
def dect():
    d = get_dect()
    d.__init__()
    return d


@pytest.fixture
def ext_alice(event, user):
    return register(event, user, "4242", "dect")


@pytest.fixture
def handset(event, user, ext_alice, dect):
    dev = Device.objects.create(event=event, owner=user, type="dect", ipei="0123456789012", omm_ppn="1")
    DeviceBinding.objects.create(extension=ext_alice, device=dev)
    dect.subs["1"] = {"ipei": dev.ipei, "number": "4242", "pin": "", "display_name": "alice", "rfp_id": "1"}
    return dev
