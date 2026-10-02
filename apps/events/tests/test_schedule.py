"""Scheduled lifecycle transitions: model validation, ``next_scheduled_transition``, the beat task, portal form,
API and template tag."""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError
from django.template import Context, Template
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.models import AuditLog
from apps.events.export import export_event
from apps.events.models import Event, validate_schedule
from apps.events.tasks import apply_scheduled_transitions

pytestmark = pytest.mark.django_db

UTC = dt.UTC
BERLIN = ZoneInfo("Europe/Berlin")


def ts(year=2030, month=8, day=12, hour=10, minute=0):
    return dt.datetime(year, month, day, hour, minute, tzinfo=UTC)


def _past(hours=1):
    return timezone.now() - dt.timedelta(hours=hours)


def _future(hours=1):
    return timezone.now() + dt.timedelta(hours=hours)


SETTINGS_POST = {
    "name": "Demo Camp", "description": "", "location": "Field", "timezone": "Europe/Berlin", "is_public": "on",
    "primary_color": "#112233", "accent_color": "#445566", "announcement": "", "sip_domain": "demo.pet.local",
    "dial_prefix": "", "default_language": "en", "max_extensions_per_user": 3, "allow_guest_extensions": "on",
    "gsm_trunk": "gsm-gateway", "cdr_retention_days": "",
}


def _settings_post(event, **extra):
    data = dict(SETTINGS_POST, start_date=event.start_date, end_date=event.end_date)
    data.update(extra)
    return data


# ------------------------------------------------------------------ validation


def test_validate_schedule_requires_increasing_order():
    validate_schedule("draft", ts(hour=8), ts(hour=9), ts(hour=10))  # ok
    validate_schedule("draft", None, ts(hour=9), None)  # gaps are fine
    with pytest.raises(ValidationError) as exc:
        validate_schedule("draft", ts(hour=9), ts(hour=9), None)
    assert set(exc.value.message_dict) == {"goes_live_at"}
    with pytest.raises(ValidationError) as exc:
        validate_schedule("draft", ts(hour=8), None, ts(hour=7))
    assert set(exc.value.message_dict) == {"archives_at"}


def test_validate_schedule_rejects_states_already_reached():
    with pytest.raises(ValidationError) as exc:
        validate_schedule("registration", ts(hour=8), ts(hour=9), None)
    assert set(exc.value.message_dict) == {"registration_opens_at"}
    with pytest.raises(ValidationError) as exc:
        validate_schedule("archived", None, None, ts())
    assert set(exc.value.message_dict) == {"archives_at"}
    validate_schedule("live", None, None, ts())  # archiving a live event is still ahead


# ------------------------------------------------------------------ next_scheduled_transition


def test_next_scheduled_transition_picks_earliest_ahead(event):
    assert event.next_scheduled_transition() is None
    event.registration_opens_at = ts(hour=7)  # stale: already in registration
    event.goes_live_at = ts(hour=12)
    event.archives_at = ts(hour=9)  # odd order, but the earliest *ahead* schedule wins
    assert event.next_scheduled_transition() == ("archived", ts(hour=9))
    event.archives_at = ts(day=13)
    assert event.next_scheduled_transition() == ("live", ts(hour=12))
    event.state = Event.State.ARCHIVED
    assert event.next_scheduled_transition() is None


def test_transition_drops_schedules_no_longer_ahead(event):
    event.goes_live_at = _future()
    event.archives_at = _future(2)
    event.save()
    event.transition("live", actor=None)
    event.refresh_from_db()
    assert event.state == "live" and event.goes_live_at is None and event.archives_at is not None


# ------------------------------------------------------------------ task


def test_task_applies_due_and_skips_future(event):
    event.goes_live_at = _past()
    event.archives_at = _future()
    event.save()
    assert apply_scheduled_transitions() == 1
    event.refresh_from_db()
    assert event.state == "live" and event.goes_live_at is None and event.archives_at is not None
    entry = AuditLog.objects.filter(event=event, action="update", message__startswith="Scheduled transition").get()
    assert entry.actor is None and entry.actor_repr == "system" and entry.changes == {"state": ["registration", "live"]}
    assert apply_scheduled_transitions() == 0  # nothing due any more


def test_task_walks_intermediate_states_in_order(event):
    event.state = Event.State.DRAFT
    event.registration_opens_at = _past(3)
    event.goes_live_at = _past(1)
    event.save()
    assert apply_scheduled_transitions() == 2
    event.refresh_from_db()
    assert event.state == "live" and event.registration_opens_at is None and event.goes_live_at is None
    states = [e.changes["state"] for e in AuditLog.objects.filter(event=event).order_by("created_at")]
    assert states == [["draft", "registration"], ["registration", "live"]]
    # a draft event with only "go live" scheduled opens registration on the way
    ev2 = Event.objects.create(name="Two", slug="two", start_date=event.start_date, end_date=event.end_date,
                               goes_live_at=_past())
    assert apply_scheduled_transitions() == 1
    ev2.refresh_from_db()
    assert ev2.state == "live"


def test_task_clears_stale_schedule_for_passed_state(event):
    event.state = Event.State.LIVE
    event.registration_opens_at = _past()
    event.save()
    assert apply_scheduled_transitions() == 0
    event.refresh_from_db()
    assert event.state == "live" and event.registration_opens_at is None
    assert not AuditLog.objects.filter(event=event).exists()


def test_task_survives_failing_transition(event, monkeypatch):
    def boom(self, new_state, actor=None, **kwargs):
        raise ValueError(f"Cannot move event from {self.state} to {new_state}")

    monkeypatch.setattr(Event, "transition", boom)
    event.goes_live_at = _past()
    event.save()
    other = Event.objects.create(name="Other", slug="other", start_date=event.start_date, end_date=event.end_date,
                                 state=Event.State.REGISTRATION, archives_at=_past())
    assert apply_scheduled_transitions() == 0
    event.refresh_from_db()
    other.refresh_from_db()
    assert event.state == "registration" and event.goes_live_at is None  # not retried forever
    assert other.archives_at is None
    assert AuditLog.objects.filter(event=event, message__contains="Scheduled transition to live failed").exists()


# ------------------------------------------------------------------ portal


def test_settings_form_saves_schedule_in_event_timezone(client, orga, event):
    client.force_login(orga)
    r = client.post("/e/demo/orga/settings/", _settings_post(
        event, goes_live_at="2030-08-12T10:00", archives_at="2030-08-20T18:30"))
    assert r.status_code == 302, r.content
    event.refresh_from_db()
    assert event.goes_live_at == dt.datetime(2030, 8, 12, 10, 0, tzinfo=BERLIN)
    assert event.goes_live_at == dt.datetime(2030, 8, 12, 8, 0, tzinfo=UTC)
    assert event.archives_at == dt.datetime(2030, 8, 20, 18, 30, tzinfo=BERLIN)
    assert event.registration_opens_at is None
    r = client.get("/e/demo/orga/settings/")
    html = r.content.decode()
    assert 'type="datetime-local"' in html and 'value="2030-08-12T10:00"' in html and "Schedule" in html
    # clearing works
    r = client.post("/e/demo/orga/settings/", _settings_post(event, goes_live_at="", archives_at=""))
    assert r.status_code == 302
    event.refresh_from_db()
    assert event.goes_live_at is None and event.archives_at is None


def test_settings_form_rejects_bad_order_and_passed_state(client, orga, event):
    client.force_login(orga)
    r = client.post("/e/demo/orga/settings/", _settings_post(
        event, goes_live_at="2030-08-12T10:00", archives_at="2030-08-12T09:00"))
    assert r.status_code == 200
    assert r.context["form"].errors.get("archives_at")
    r = client.post("/e/demo/orga/settings/", _settings_post(event, registration_opens_at="2030-08-12T10:00"))
    assert r.status_code == 200
    assert r.context["form"].errors.get("registration_opens_at")
    event.refresh_from_db()
    assert event.goes_live_at is None and event.registration_opens_at is None


def test_dashboard_shows_next_transition(client, orga, event):
    client.force_login(orga)
    assert b"Next:" not in client.get("/e/demo/orga/").content
    event.goes_live_at = dt.datetime(2030, 8, 12, 8, 0, tzinfo=UTC)
    event.save()
    html = client.get("/e/demo/orga/").content.decode()
    assert "Next: Live on 12 Aug 2030 10:00" in html  # rendered in Europe/Berlin


def test_next_transition_tag_empty_without_schedule(event):
    out = Template("{% load events_tags %}[{% next_transition event %}]").render(Context({"event": event}))
    assert out == "[]"


# ------------------------------------------------------------------ API


def _api(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def test_api_patch_schedule_by_orga(event, orga):
    c = _api(orga)
    r = c.patch("/api/v1/events/demo/", {"goes_live_at": "2030-08-12T10:00:00+02:00"}, format="json")
    assert r.status_code == 200, r.content
    assert r.json()["next_scheduled_transition"] == {"state": "live", "at": "2030-08-12T08:00:00+00:00"}
    event.refresh_from_db()
    assert event.goes_live_at == dt.datetime(2030, 8, 12, 8, 0, tzinfo=UTC)
    # order is validated against the stored values too
    r = c.patch("/api/v1/events/demo/", {"archives_at": "2030-08-12T09:00:00+02:00"}, format="json")
    assert r.status_code == 400 and "archives_at" in r.json()
    r = c.patch("/api/v1/events/demo/", {"registration_opens_at": "2030-08-01T09:00:00Z"}, format="json")
    assert r.status_code == 400 and "registration_opens_at" in r.json()
    r = c.patch("/api/v1/events/demo/", {"goes_live_at": None}, format="json")
    assert r.status_code == 200 and r.json()["next_scheduled_transition"] is None


def test_api_patch_schedule_forbidden_for_plain_user(event, user, member):
    c = _api(user)
    r = c.patch("/api/v1/events/demo/", {"goes_live_at": "2030-08-12T10:00:00Z"}, format="json")
    assert r.status_code == 403
    r = c.get("/api/v1/events/demo/")
    assert r.status_code == 200 and r.json()["goes_live_at"] is None and "next_scheduled_transition" in r.json()


# ------------------------------------------------------------------ clone / export


def test_clone_does_not_copy_schedule_but_export_does(event):
    event.goes_live_at = ts()
    event.save()
    new = event.clone(name="Next", slug="next", start_date=event.start_date, end_date=event.end_date)
    assert new.goes_live_at is None and new.registration_opens_at is None and new.archives_at is None
    data = export_event(event)
    assert data["event"]["goes_live_at"] == "2030-08-12T10:00:00+00:00" and data["event"]["archives_at"] is None
