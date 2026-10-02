"""Number history: timeline across events with visibility rules, audit merge, helpdesk card, API."""
import datetime as dt

import pytest
from rest_framework.test import APIClient

from apps.events.models import Event, EventMembership
from apps.extensions import services
from apps.extensions.history import looks_like_number, number_history, visible_events
from apps.extensions.models import Extension
from apps.numbering.models import NumberPlan

pytestmark = pytest.mark.django_db


@pytest.fixture
def other_event(db):
    today = dt.date.today()
    ev = Event.objects.create(name="Last Year", slug="lastyear", state=Event.State.ARCHIVED,
                              start_date=today - dt.timedelta(days=365), end_date=today - dt.timedelta(days=360))
    NumberPlan.objects.create(event=ev, min_length=4, max_length=4)
    return ev


@pytest.fixture
def third_event(db):
    today = dt.date.today()
    ev = Event.objects.create(name="Secret Camp", slug="secret", state=Event.State.LIVE, is_public=False,
                              start_date=today, end_date=today + dt.timedelta(days=2))
    NumberPlan.objects.create(event=ev, min_length=4, max_length=4)
    return ev


def _seed(event, other_event, third_event, user, other_user, orga):
    """4242 in three events: deleted + re-registered here, one in last year's event, one in a hidden event."""
    old = Extension.objects.create(event=other_event, number="4242", owner=other_user, state="active", type="dect")
    first = services.register(event, user, "4242", "dect", force_active=True)
    services.delete(first, orga)
    current = services.register(event, other_user, "4242", "sip", force_active=True, ported_from=old)
    hidden = Extension.objects.create(event=third_event, number="4242", owner=user, state="active", type="dect")
    return old, first, current, hidden


def test_looks_like_number():
    assert looks_like_number("4242") and looks_like_number(" 12 ")
    assert not looks_like_number("alice") and not looks_like_number("") and not looks_like_number("4242a")


def test_visible_events(event, other_event, third_event, orga, admin, user):
    assert set(visible_events(admin)) == {event, other_event, third_event}
    assert set(visible_events(orga, event)) == {event}
    EventMembership.objects.create(event=other_event, user=orga, role="helpdesk")
    assert set(visible_events(orga, event)) == {event, other_event}
    assert set(visible_events(user, event)) == {event}  # not staff anywhere, requested event still included


def test_number_history_orga_sees_own_and_staffed_events_only(event, other_event, third_event, user, other_user,
                                                              orga):
    old, first, current, hidden = _seed(event, other_event, third_event, user, other_user, orga)
    EventMembership.objects.create(event=other_event, user=orga, role="helpdesk")

    h = number_history(event, "4242", orga)
    ids = [x["id"] for x in h["extensions"]]
    assert ids == [str(current.pk), str(first.pk), str(old.pk)]  # newest first, hidden event excluded
    assert h["other_events"] == ["lastyear"]
    cur, dead, prev = h["extensions"]
    assert cur["is_current_event"] and cur["state"] == "active" and cur["owner"]["username"] == "bob"
    assert cur["ported_from"] == {"id": str(old.pk), "event": "lastyear", "number": "4242", "state": "active"}
    assert dead["state"] == "deleted" and dead["deleted_at"] is not None and dead["owner"]["email"] == user.email
    assert prev["event"] == {"slug": "lastyear", "name": "Last Year"} and not prev["is_current_event"]
    assert prev["ported_to"] == [{"id": str(current.pk), "event": "demo", "number": "4242", "state": "active"}]

    # audit: create/delete/create of the two demo extensions, actor resolved, none from the hidden event
    actions = [(a["action"], a["extension"]["id"]) for a in h["audit"]]
    assert ("delete", str(first.pk)) in actions and ("create", str(current.pk)) in actions
    assert all(a["extension"]["id"] in ids for a in h["audit"])
    deleted = next(a for a in h["audit"] if a["action"] == "delete")
    assert deleted["actor"] == {"id": str(orga.pk), "username": "orga", "email": orga.email}
    assert deleted["changes"] == {"state": ["active", "deleted"]}

    # merged timeline sorted newest first
    ats = [t["at"] for t in h["timeline"]]
    assert ats == sorted(ats, reverse=True)
    assert {t["kind"] for t in h["timeline"]} == {"extension", "audit"}
    assert len(h["timeline"]) == len(h["extensions"]) + len(h["audit"])


def test_number_history_superuser_sees_everything(event, other_event, third_event, user, other_user, orga, admin):
    _seed(event, other_event, third_event, user, other_user, orga)
    h = number_history(event, "4242", admin)
    assert sorted(x["event"]["slug"] for x in h["extensions"]) == ["demo", "demo", "lastyear", "secret"]
    assert h["other_events"] == ["lastyear", "secret"]


def test_number_history_unknown_number(event, orga):
    h = number_history(event, "8888", orga)
    assert h["extensions"] == [] and h["audit"] == [] and h["timeline"] == [] and h["number"] == "8888"


# --------------------------------------------------------------------------- helpdesk card

def test_helpdesk_shows_history_card(client, event, other_event, third_event, user, other_user, orga):
    _seed(event, other_event, third_event, user, other_user, orga)
    client.force_login(orga)
    r = client.get(f"/e/{event.slug}/orga/helpdesk/", {"q": "4242"})
    assert r.status_code == 200
    html = r.content.decode()
    assert "History of 4242" in html and "Timeline" in html
    badge = '<span class="badge badge-muted">{}</span>'
    assert badge.format("lastyear") not in html and badge.format("secret") not in html  # orga staffs only demo
    # a helpdesk member of last year's event sees that event too
    EventMembership.objects.create(event=other_event, user=orga, role="helpdesk")
    html = client.get(f"/e/{event.slug}/orga/helpdesk/", {"q": "4242"}).content.decode()
    assert badge.format("lastyear") in html and badge.format("secret") not in html
    # text queries and unused numbers show no card
    assert "History of" not in client.get(f"/e/{event.slug}/orga/helpdesk/", {"q": "alice"}).content.decode()
    assert "History of" not in client.get(f"/e/{event.slug}/orga/helpdesk/", {"q": "8888"}).content.decode()


def test_helpdesk_history_for_helpdesk_role(client, event, user, member, other_user, orga):
    services.register(event, other_user, "4242", "dect", force_active=True)
    member.role = "helpdesk"
    member.save()
    client.force_login(user)
    r = client.get(f"/e/{event.slug}/orga/helpdesk/", {"q": "4242"})
    assert r.status_code == 200 and "History of 4242" in r.content.decode()


# --------------------------------------------------------------------------- API

def test_api_history(event, other_event, third_event, user, other_user, orga, admin):
    old, first, current, hidden = _seed(event, other_event, third_event, user, other_user, orga)
    c = APIClient()
    c.force_authenticate(orga)
    r = c.get("/api/v1/extensions/history/", {"event": "demo", "number": "4242"})
    assert r.status_code == 200, r.content
    data = r.json()
    assert data["number"] == "4242" and data["event"] == {"slug": "demo", "name": "Demo Camp"}
    assert [x["id"] for x in data["extensions"]] == [str(current.pk), str(first.pk)]
    assert data["extensions"][0]["owner"] == {"id": str(other_user.pk), "username": "bob", "email": other_user.email}
    assert isinstance(data["extensions"][0]["created_at"], str) and "T" in data["extensions"][0]["created_at"]
    assert data["audit"] and all(isinstance(a["created_at"], str) for a in data["audit"])
    assert data["timeline"][0]["at"] >= data["timeline"][-1]["at"]

    c.force_authenticate(admin)
    data = c.get("/api/v1/extensions/history/", {"event": "demo", "number": "4242"}).json()
    assert len(data["extensions"]) == 4


def test_api_history_permissions_and_validation(event, user, member, orga):
    c = APIClient()
    c.force_authenticate(user)
    assert c.get("/api/v1/extensions/history/", {"event": "demo", "number": "4242"}).status_code == 403
    member.role = "helpdesk"
    member.save()
    assert c.get("/api/v1/extensions/history/", {"event": "demo", "number": "4242"}).status_code == 200
    c.force_authenticate(orga)
    assert c.get("/api/v1/extensions/history/", {"event": "nope", "number": "4242"}).status_code == 404
    assert c.get("/api/v1/extensions/history/", {"event": "demo"}).status_code == 400
    assert c.get("/api/v1/extensions/history/", {"event": "demo", "number": "abc"}).status_code == 400
    assert APIClient().get("/api/v1/extensions/history/", {"event": "demo", "number": "4242"}).status_code in (401,
                                                                                                             403)
