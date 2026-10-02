"""Event GSM fields (``has_gsm`` / ``gsm_trunk``) and their interplay with the device endpoint registry."""
import pytest

from apps.devices import endpoint_types
from apps.events.models import Event

pytestmark = pytest.mark.django_db


def test_event_gsm_defaults(event):
    ev = Event.objects.get(pk=event.pk)
    assert ev.has_gsm is False and ev.gsm_trunk == "gsm-gateway"
    assert endpoint_types.get("gsm").enabled_for(ev) is False


def test_event_gsm_enabled(event):
    event.has_gsm = True
    event.gsm_trunk = "osmo-trunk"
    event.save()
    ev = Event.objects.get(pk=event.pk)
    assert ev.has_gsm is True and ev.gsm_trunk == "osmo-trunk"
    assert endpoint_types.get("gsm").enabled_for(ev) is True


def test_clone_does_not_copy_gsm_flag_by_default(event):
    """GSM is site infrastructure, not a number-plan property: a cloned event starts without it."""
    event.has_gsm = True
    event.save()
    new = event.clone(name="Next", slug="next", start_date=event.start_date, end_date=event.end_date)
    assert new.has_gsm is False and new.gsm_trunk == "gsm-gateway"
