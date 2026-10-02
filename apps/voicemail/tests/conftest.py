"""Shared fixtures for the voicemail tests."""
import pytest

from apps.dect import get_dect
from apps.extensions.services import register
from apps.pbx import get_pbx


@pytest.fixture
def pbx():
    p = get_pbx()
    p.reset()
    return p


@pytest.fixture
def dect():
    d = get_dect()
    d.mwi.clear()
    return d


@pytest.fixture
def ext_alice(event, user):
    return register(event, user, "4242", "dect")


@pytest.fixture
def ext_bob(event, other_user):
    return register(event, other_user, "4300", "dect", display_name="Bob Builder")


@pytest.fixture
def wav_file(tmp_path):
    p = tmp_path / "msg0000.wav"
    p.write_bytes(b"RIFF" + b"\x00" * 40 + b"WAVEfmt " + b"\x01" * 64)
    return p
