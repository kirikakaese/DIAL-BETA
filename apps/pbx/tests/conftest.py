"""Shared fixtures for PBX tests."""
import pytest

from apps.devices.models import Device, DeviceBinding
from apps.extensions.models import Extension, ExtensionType
from apps.pbx.backends.asterisk import AsteriskPBX


@pytest.fixture
def pbx():
    p = AsteriskPBX()
    p.use_ami = False  # never touch the network in unit tests
    return p


def make_device(event, username, *, dtype="sip", owner=None, transport="udp", name=""):
    return Device.objects.create(event=event, owner=owner, type=dtype, sip_username=username,
                                 sip_password="secretpw", sip_transport=transport, name=name,
                                 state=Device.State.SUBSCRIBED)


def make_extension(event, number, *, etype=ExtensionType.SIP, owner=None, **fields):
    return Extension.objects.create(event=event, number=number, type=etype, owner=owner,
                                    state=Extension.State.ACTIVE, **fields)


def bind(ext, device, priority=0, ring_delay=0):
    return DeviceBinding.objects.create(extension=ext, device=device, priority=priority, ring_delay=ring_delay)


@pytest.fixture
def ext_two_devices(event, user):
    ext = make_extension(event, "4242", owner=user, display_name="Alice")
    d1 = make_device(event, "demo-aaaa", dtype="dect", owner=user)
    d2 = make_device(event, "demo-bbbb", dtype="sip", owner=user)
    bind(ext, d1, priority=0)
    bind(ext, d2, priority=1, ring_delay=5)
    return ext
