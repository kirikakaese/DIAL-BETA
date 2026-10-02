"""REST API: read-only listings scoped to staff events, sync trigger, coverage."""
import pytest
from rest_framework.test import APIClient

from apps.dect import services
from apps.dect.provisioning import provision_dect_for_extension
from apps.events.models import EventMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def synced(dect, event, dect_extension, dect_device):
    provision_dect_for_extension(dect_extension)
    dect.set_rfp("2", connected=False)
    services.sync_infrastructure(event)
    return event


def _results(r):
    d = r.json()
    return d["results"] if isinstance(d, dict) and "results" in d else d


def test_rfps_list_for_orga_and_filters(synced, orga):
    c = APIClient()
    c.force_authenticate(orga)
    r = c.get("/api/v1/dect/rfps/?event__slug=demo")
    assert r.status_code == 200
    rows = _results(r)
    assert len(rows) == 6 and {"status", "cluster", "handsets", "omm_id"} <= set(rows[0])
    down = _results(c.get("/api/v1/dect/rfps/?event__slug=demo&connected=false"))
    assert [x["name"] for x in down] == ["RFP-Foodcourt"]
    assert _results(c.get("/api/v1/dect/rfps/?event__slug=other")) == []


def test_lists_hidden_from_plain_user_and_anonymous(synced, user, member):
    c = APIClient()
    assert c.get("/api/v1/dect/rfps/").status_code in (401, 403)
    c.force_authenticate(user)
    assert _results(c.get("/api/v1/dect/rfps/")) == []
    assert _results(c.get("/api/v1/dect/handsets/")) == []
    assert c.post("/api/v1/dect/sync/?event=demo").status_code == 403
    assert c.get("/api/v1/dect/coverage/?event=demo").status_code == 403


def test_helpdesk_can_read_but_not_sync(synced, other_user):
    EventMembership.objects.create(event=synced, user=other_user, role="helpdesk")
    c = APIClient()
    c.force_authenticate(other_user)
    assert len(_results(c.get("/api/v1/dect/clusters/"))) == 2
    hs = _results(c.get("/api/v1/dect/handsets/?event__slug=demo"))
    assert len(hs) == 1 and hs[0]["ipei"] == "0123456789012" and hs[0]["extensions"] == ["4242"]
    alerts = _results(c.get("/api/v1/dect/alerts/?event__slug=demo&open=1"))
    assert {a["kind"] for a in alerts} == {"rfp.down", "sync.degraded"}
    cov = c.get("/api/v1/dect/coverage/?event=demo").json()
    assert cov["totals"]["rfps"] == 6 and cov["totals"]["down"] == 1
    assert c.post("/api/v1/dect/sync/?event=demo").status_code == 403


def test_sync_endpoint(synced, orga, dect):
    c = APIClient()
    c.force_authenticate(orga)
    dect.set_rfp("2", connected=True)
    r = c.post("/api/v1/dect/sync/?event=demo")
    assert r.status_code == 200 and r.json()["rfps"] == 6
    assert c.post("/api/v1/dect/sync/?event=nope").status_code == 404
    assert c.get("/api/v1/dect/rfps/?event__slug=demo&connected=false").json()["results"] == []
