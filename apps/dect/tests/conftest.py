import pytest

from apps.dect import get_dect, reset_dect_cache
from apps.devices.models import Device, DeviceBinding
from apps.extensions import services as ext_services


@pytest.fixture
def dect():
    """Fresh in-memory DummyDECT for every test (the adapter is lru_cached)."""
    reset_dect_cache()
    from apps.pbx import reset_pbx_cache

    reset_pbx_cache()
    yield get_dect()
    reset_dect_cache()


@pytest.fixture
def dect_extension(event, user, member):
    return ext_services.register(event, user, "4242", "dect")


@pytest.fixture
def dect_device(event, user, dect_extension):
    device = Device.objects.create(event=event, owner=user, type="dect", ipei="0123456789012", name="orange")
    DeviceBinding.objects.create(extension=dect_extension, device=device)
    return device
