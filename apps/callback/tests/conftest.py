"""Shared fixtures for the callback tests."""
import pytest

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
