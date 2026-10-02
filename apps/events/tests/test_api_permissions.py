"""REST API: event lifecycle is orga-driven, but creating/cloning/deleting events is global-admin only."""
import pytest
from rest_framework.test import APIClient

from apps.events.models import Event

pytestmark = pytest.mark.django_db

NEW = {"name": "Camp 2031", "slug": "camp31", "start_date": "2031-08-01", "end_date": "2031-08-05"}


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def test_orga_can_read_and_transition_but_not_create(event, orga):
    c = _client(orga)
    assert c.get("/api/v1/events/demo/").status_code == 200
    r = c.post("/api/v1/events/demo/transition/", {"state": "live"}, format="json")
    assert r.status_code == 200 and r.json()["state"] == "live"
    r = c.post("/api/v1/events/", NEW, format="json")
    assert r.status_code == 403 and "global admins" in r.json()["detail"]
    r = c.post("/api/v1/events/demo/clone/", NEW, format="json")
    assert r.status_code == 403
    r = c.delete("/api/v1/events/demo/")
    assert r.status_code == 403
    assert Event.objects.filter(slug="demo").exists() and not Event.objects.filter(slug="camp31").exists()


def test_admin_creates_clones_and_deletes(event, admin, orga):
    c = _client(admin)
    r = c.post("/api/v1/events/", NEW, format="json")
    assert r.status_code == 201, r.content
    new = Event.objects.get(slug="camp31")
    assert new.state == "draft" and new.number_plan is not None
    assert new.memberships.filter(user=admin, role="admin").exists()
    r = c.post("/api/v1/events/demo/clone/", {**NEW, "slug": "camp32"}, format="json")
    assert r.status_code == 201 and r.json()["state"] == "draft"
    clone = Event.objects.get(slug="camp32")
    assert clone.memberships.filter(user=orga, role="orga").exists()
    assert clone.number_plan.ranges.count() == event.number_plan.ranges.count()
    assert c.delete("/api/v1/events/camp32/").status_code == 204
    assert not Event.objects.filter(slug="camp32").exists()
