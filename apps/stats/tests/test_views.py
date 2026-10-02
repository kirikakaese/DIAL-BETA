"""Portal views: dashboard (helpdesk/orga), CSV export, my calls + GDPR."""
import json

import pytest
from django.urls import reverse

from apps.stats import services
from apps.stats.models import CallRecord

from .conftest import cdr

pytestmark = [pytest.mark.django_db, pytest.mark.urls("apps.stats.tests.urls_stub")]


def _url(name, event):
    return reverse(f"stats:{name}", args=[event.slug])


def test_index_for_orga(client, event, orga, ext_alice, ext_bob):
    services.ingest_cdr(event, cdr(uniqueid="v-1"))
    client.force_login(orga)
    r = client.get(_url("index", event) + "?hours=24")
    assert r.status_code == 200
    body = r.content.decode()
    assert 'id="chart-data"' in body and "stats/charts.js" in body
    assert "Busiest extensions" in body and "4242" in body and "ANSWERED" in body
    assert "Export CSV" in body and "Privacy mode" not in body


def test_index_privacy_notice_and_bad_hours(client, event, orga):
    event.cdr_aggregate_only = True
    event.save()
    client.force_login(orga)
    r = client.get(_url("index", event) + "?hours=abc")
    assert r.status_code == 200 and "Privacy mode" in r.content.decode()


def test_index_forbidden_for_plain_user_and_anonymous(client, event, user, member):
    client.force_login(user)
    assert client.get(_url("index", event)).status_code == 403
    client.logout()
    assert client.get(_url("index", event)).status_code in (302, 403)


def test_index_404_when_feature_disabled(client, event, orga):
    event.settings = {"disabled_features": ["stats"]}
    event.save()
    client.force_login(orga)
    assert client.get(_url("index", event)).status_code == 404


def test_export_csv_orga_only(client, event, orga, user, member, ext_alice, ext_bob):
    services.ingest_cdr(event, cdr(uniqueid="csv-1"))
    client.force_login(user)
    assert client.get(_url("export_csv", event)).status_code == 403
    client.force_login(orga)
    r = client.get(_url("export_csv", event))
    assert r.status_code == 200 and r["Content-Type"].startswith("text/csv")
    assert "csv-1" in r.content.decode()


def test_mine_and_gdpr(client, event, user, member, ext_alice, ext_bob):
    services.ingest_cdr(event, cdr(uniqueid="m-1"))
    client.force_login(user)
    r = client.get(_url("mine", event))
    assert r.status_code == 200
    body = r.content.decode()
    assert "4300" in body and "outbound" in body and "Download my data" in body

    r = client.get(_url("gdpr_export", event))
    assert r.status_code == 200 and "attachment" in r["Content-Disposition"]
    assert len(json.loads(r.content)["calls"]) == 1

    # delete requires confirmation
    r = client.post(_url("gdpr_delete", event), {})
    assert r.status_code == 302 and CallRecord.objects.get().src_number == "4242"
    r = client.post(_url("gdpr_delete", event), {"confirm": "yes"})
    assert r.status_code == 302 and CallRecord.objects.get().src_number == "anon"


def test_mine_aggregate_only(client, event, user, member):
    event.cdr_aggregate_only = True
    event.save()
    client.force_login(user)
    r = client.get(_url("mine", event))
    assert r.status_code == 200 and "privacy mode" in r.content.decode()
