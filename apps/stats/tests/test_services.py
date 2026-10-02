"""Services: ingest, aggregation, retention, dashboard, GDPR, tasks."""
import datetime as dt

import pytest
from django.utils import timezone

from apps.dect.models import RFP, RFPStatusSample
from apps.stats import services, tasks
from apps.stats.models import CallRecord, ExtensionStat, HourlyStat

from .conftest import cdr

pytestmark = pytest.mark.django_db


def test_ingest_creates_record_and_is_idempotent(event, ext_alice, ext_bob):
    rec = services.ingest_cdr(event, cdr())
    assert rec is not None and rec.pk is not None
    assert rec.src_extension == ext_alice and rec.dst_extension == ext_bob
    assert rec.src_type == "dect" and rec.dst_type == "dect"
    assert rec.answered and rec.billsec == 42

    again = services.ingest_cdr(event, cdr())
    assert again.pk == rec.pk
    assert CallRecord.objects.count() == 1

    hs = HourlyStat.objects.get(event=event)
    assert hs.calls == 1 and hs.answered == 1 and hs.total_billsec == 42
    assert hs.by_disposition == {"ANSWERED": 1} and hs.by_type == {"dect": 1}
    assert ExtensionStat.objects.get(extension=ext_alice).outbound == 1
    assert ExtensionStat.objects.get(extension=ext_bob).inbound == 1


def test_ingest_ignores_garbage_and_disabled_feature(event, settings):
    assert services.ingest_cdr(event, {}) is None
    assert services.ingest_cdr(event, "nope") is None
    event.settings = {"disabled_features": ["stats"]}
    event.save()
    assert services.ingest_cdr(event, cdr()) is None
    assert CallRecord.objects.count() == 0


def test_aggregate_only_stores_no_record_but_bumps_hourly(event, ext_alice, ext_bob):
    event.cdr_aggregate_only = True
    event.save()
    rec = services.ingest_cdr(event, cdr(uniqueid="agg-1"))
    assert rec is not None and rec.pk is None
    services.ingest_cdr(event, cdr(uniqueid="agg-1"))  # duplicate notify -> no double count
    services.ingest_cdr(event, cdr(uniqueid="agg-2", src="4300", dst="4242", disposition="NO ANSWER", billsec=0))
    assert CallRecord.objects.count() == 0
    hs = HourlyStat.objects.get(event=event)
    assert hs.calls == 2 and hs.answered == 1 and hs.unique_callers == 2
    assert services.my_calls(ext_alice.owner, event=event).count() == 0
    assert b"hour,calls" in services.export_csv(event)


def test_aggregate_hourly_rebuilds_rows(event, ext_alice, ext_bob):
    now = timezone.now()
    rfp = RFP.objects.create(event=event, omm_id=1, name="RFP-1")
    for i in range(3):
        services.ingest_cdr(event, cdr(uniqueid=f"h-{i}", start=(now - dt.timedelta(hours=i)).isoformat(), rfp="RFP-1"))
    HourlyStat.objects.all().delete()
    hour = services._hour(now)
    RFPStatusSample.objects.create(rfp=rfp, at=hour + dt.timedelta(minutes=1), connected=True, synced=True,
                                   active_calls=2)
    RFPStatusSample.objects.create(rfp=rfp, at=hour + dt.timedelta(minutes=2), connected=True, synced=True,
                                   active_calls=4)
    assert services.aggregate_hourly(event) == 3
    stats = list(HourlyStat.objects.filter(event=event))
    assert sum(s.calls for s in stats) == 3
    current = HourlyStat.objects.get(event=event, hour=hour)
    entry = current.by_rfp[str(rfp.pk)]
    assert entry["calls"] == 1 and entry["erlang_dect"] == 3.0 and entry["samples"] == 2
    # task runs for live events (registration counts as live)
    assert tasks.aggregate_hourly()["demo"] == 3


def test_enforce_retention(event, ext_alice, ext_bob, settings):
    old = timezone.now() - dt.timedelta(days=40)
    services.ingest_cdr(event, cdr(uniqueid="old", start=old.isoformat()))
    services.ingest_cdr(event, cdr(uniqueid="new"))
    settings.PET_CDR_RETENTION_DAYS = 30
    assert services.enforce_retention(event) == 1
    assert CallRecord.objects.get().uniqueid == "new"
    event.cdr_retention_days = 0  # keep forever
    event.save()
    assert services.enforce_retention(event) == 0
    assert tasks.enforce_retention() == {}


def test_dashboard_data_keys(event, ext_alice, ext_bob):
    services.ingest_cdr(event, cdr(uniqueid="d-1"))
    services.ingest_cdr(event, cdr(uniqueid="d-2", disposition="NO ANSWER", billsec=0))
    RFP.objects.create(event=event, omm_id=7, name="Hall A")
    d = services.dashboard_data(event, hours=24)
    for key in ("event", "hours", "from", "to", "aggregate_only", "retention_days", "series", "totals",
                "dispositions", "by_type", "top_extensions", "rfps", "heatmap", "records_kept"):
        assert key in d
    assert len(d["series"]) == 24
    assert d["totals"]["calls"] == 2 and d["totals"]["answered"] == 1 and d["totals"]["answer_rate"] == 50
    assert d["totals"]["avg_duration"] == 42 and d["totals"]["unique_callers"] == 1
    assert d["dispositions"] == {"ANSWERED": 1, "NO ANSWER": 1}
    assert d["top_extensions"][0]["total"] == 2
    assert d["rfps"][0]["name"] == "Hall A" and d["heatmap"][0]["name"] == "Hall A"
    assert d["records_kept"] == 2


def test_top_extensions_respect_phonebook_privacy(event, ext_alice, ext_bob):
    ext_alice.in_phonebook = False
    ext_alice.save()
    services.ingest_cdr(event, cdr(uniqueid="p-1"))
    labels = {x["label"]: x for x in services.dashboard_data(event)["top_extensions"]}
    assert "private" in labels and labels["private"]["private"] is True
    assert any(label.startswith("4300") for label in labels)
    assert not any("4242" in label for label in labels)


def test_gdpr_export_and_delete(event, user, other_user, ext_alice, ext_bob):
    services.ingest_cdr(event, cdr(uniqueid="g-1"))
    services.ingest_cdr(event, cdr(uniqueid="g-2", src="4300", dst="4242"))
    data = services.gdpr_export(user)
    assert data["user"]["username"] == user.username
    assert [e["number"] for e in data["extensions"]] == ["4242"]
    assert len(data["calls"]) == 2
    assert {c["direction"] for c in data["calls"]} == {"outbound", "inbound"}

    assert services.gdpr_delete(user) == 2
    assert services.gdpr_export(user)["calls"] == []
    for rec in CallRecord.objects.all():
        assert "4242" not in (rec.src_number, rec.dst_number) and "anon" in (rec.src_number, rec.dst_number)
        assert rec.raw == {}
    assert not ExtensionStat.objects.filter(extension=ext_alice).exists()
    # bob's side untouched
    assert len(services.gdpr_export(other_user)["calls"]) == 2
    assert HourlyStat.objects.get(event=event).calls == 2


def test_export_csv(event, ext_alice, ext_bob):
    services.ingest_cdr(event, cdr(uniqueid="c-1"))
    body = services.export_csv(event).decode()
    lines = body.strip().splitlines()
    assert lines[0].startswith("started_at,") and len(lines) == 2 and ",c-1" in lines[1]
