"""Shared fixtures for the callgroups tests."""
import pytest

from apps.callgroups import services
from apps.extensions.services import register
from apps.pbx import get_pbx


@pytest.fixture
def pbx():
    p = get_pbx()
    p.reset()
    return p


@pytest.fixture
def ext_alice(event, user):
    return register(event, user, "4242", "dect")


@pytest.fixture
def ext_bob(event, other_user):
    return register(event, other_user, "4300", "dect")


@pytest.fixture
def ext_carol(event, orga):
    return register(event, orga, "4301", "sip")


@pytest.fixture
def group(event, user, member):
    return services.create_group(event, user, "4400", "Infodesk", "ringall")


@pytest.fixture
def full_group(group, ext_alice, ext_bob, ext_carol, user):
    services.add_member(group, ext_alice, user)
    services.add_member(group, ext_bob, user)
    services.add_member(group, ext_carol, user)
    return group
