"""Shared fixtures for the stats tests."""
import pytest
from django.core.cache import cache

from apps.extensions.services import register


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def ext_alice(event, user):
    return register(event, user, "4242", "dect")


@pytest.fixture
def ext_bob(event, other_user):
    return register(event, other_user, "4300", "dect")


def cdr(src="4242", dst="4300", uniqueid="ast-1", billsec=42, disposition="ANSWERED", **extra):
    from django.utils import timezone

    rec = {"src": src, "dst": dst, "start": timezone.now().isoformat(), "duration": billsec + 5,
           "billsec": billsec, "disposition": disposition, "uniqueid": uniqueid, "channel": f"PJSIP/{src}-0001",
           "dstchannel": f"PJSIP/{dst}-0002"}
    rec.update(extra)
    return rec
