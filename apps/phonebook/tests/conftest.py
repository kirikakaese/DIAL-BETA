"""Shared fixtures for the phonebook tests."""
import pytest

from apps.extensions.services import register


@pytest.fixture
def ext_alice(event, user):
    return register(event, user, "4242", "dect", location_hint="Hackcenter, table 12")


@pytest.fixture
def ext_bob(event, other_user):
    return register(event, other_user, "4300", "dect", display_name="Bob Builder")


@pytest.fixture
def ext_hidden(event, other_user):
    return register(event, other_user, "4301", "dect", in_phonebook=False)


@pytest.fixture
def ext_group(event, orga):
    return register(event, orga, "4400", "group", display_name="Infodesk")
