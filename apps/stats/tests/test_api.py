"""REST API: summary/hourly/extensions/rfps (helpdesk/orga), me/export + DELETE me, PBX cdr hook."""
import pytest
from rest_framework.test import APIClient

from apps.dect.models import RFP
from apps.stats import services
from apps.stats.models import CallRecord

from .conftest import cdr

pytestmark = pytest.mark.django_db


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def test_summary_for_orga(event, orga, ext_alice, ext_bob):
    services.ingest_cdr(event, cdr(uniqueid="a-1"))
    r = _client(orga).get("/api/v1/stats/summary/?event=demo&hours=12")
    assert r.status_code == 200, r.content
    d = r.json()
    assert d["hours"] == 12 and d["totals"]["calls"] == 1 and d["dispositions"] == {"ANSWERED": 1}
    assert d["top_extensions"][0]["total"] == 1 and len(d["top_extensions"]) == 2


def test_hourly_extensions_rfps(event, orga, ext_alice, ext_bob):
    RFP.objects.create(event=event, omm_id=3, name="Tent")
    services.ingest_cdr(event, cdr(uniqueid="a-2", rfp="Tent"))
    c = _client(orga)
    r = c.get("/api/v1/stats/hourly/?event=demo&hours=6")
    assert r.status_code == 200 and len(r.json()["series"]) == 6
    r = c.get("/api/v1/stats/extensions/?event=demo")
    assert r.status_code == 200 and len(r.json()["top_extensions"]) == 2
    r = c.get("/api/v1/stats/rfps/?event=demo")
    assert r.status_code == 200
    assert r.json()["rfps"][0]["name"] == "Tent" and r.json()["rfps"][0]["calls"] == 1
    assert r.json()["heatmap"][0]["calls"] == 1


def test_event_endpoints_forbidden_for_plain_user(event, user, member):
    c = _client(user)
    for ep in ("summary", "hourly", "extensions", "rfps"):
        assert c.get(f"/api/v1/stats/{ep}/?event=demo").status_code == 403
    assert c.get("/api/v1/stats/summary/").status_code == 403
    assert c.get("/api/v1/stats/summary/?event=nope").status_code == 404
    assert APIClient().get("/api/v1/stats/summary/?event=demo").status_code in (401, 403)


def test_me_export_and_delete(event, user, ext_alice, ext_bob):
    services.ingest_cdr(event, cdr(uniqueid="me-1"))
    c = _client(user)
    r = c.get("/api/v1/stats/me/export/")
    assert r.status_code == 200 and len(r.json()["calls"]) == 1
    r = c.delete("/api/v1/stats/me/")
    assert r.status_code == 200 and r.json()["anonymized"] == 1
    assert CallRecord.objects.get().src_number == "anon"
    assert c.get("/api/v1/stats/me/").status_code == 405


def test_pbx_cdr_hook_ingests(event, ext_alice, ext_bob):
    from apps.pbx.api import hook_secret

    c = APIClient()
    payload = {"event": "demo", **cdr(uniqueid="hook-1")}
    r = c.post("/api/v1/pbx/hooks/cdr/", payload, format="json", HTTP_X_PET_PBX_SECRET=hook_secret())
    assert r.status_code == 200, r.content
    assert r.json()["handled"] is True
    assert CallRecord.objects.filter(uniqueid="hook-1").exists()
