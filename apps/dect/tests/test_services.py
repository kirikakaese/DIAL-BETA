"""Monitoring services: sync, alerts + notifications, site survey, coverage."""
from unittest import mock

import pytest
from django.core import mail
from django.test import override_settings

from apps.dect import services
from apps.dect.models import RFP, Alert, RFPStatusSample, SiteSurveyLog, SyncCluster
from apps.dect.provisioning import provision_dect_for_extension
from apps.dect.tasks import poll_infrastructure, purge_old_samples
from apps.devices.models import Device

pytestmark = pytest.mark.django_db


def test_sync_creates_rfps_clusters_samples(dect, event):
    res = services.sync_infrastructure(event)
    assert res["ok"] and res["rfps"] == 6 and res["clusters"] == 2
    assert RFP.objects.filter(event=event).count() == 6
    assert set(SyncCluster.objects.filter(event=event).values_list("cluster_id", flat=True)) == {"1", "2"}
    assert RFPStatusSample.objects.count() == 6
    rfp = RFP.objects.get(event=event, omm_id="1")
    assert rfp.connected and rfp.synced and rfp.cluster.cluster_id == "1" and rfp.location.startswith("Main stage")
    assert rfp.last_seen_at and rfp.last_state_change
    # dummy RFP 6 is deliberately unsynced → degraded warning
    assert Alert.objects.filter(event=event, kind="sync.degraded", resolved_at__isnull=True).count() == 1
    assert SyncCluster.objects.get(event=event, cluster_id="2").health == "degraded"
    # second run is idempotent: no duplicate alerts
    services.sync_infrastructure(event)
    assert Alert.objects.filter(event=event, kind="sync.degraded").count() == 1


def test_sync_alerts_on_down_and_resolves_on_up(dect, event):
    services.sync_infrastructure(event)
    dect.set_rfp("2", connected=False)
    services.sync_infrastructure(event)
    rfp = RFP.objects.get(event=event, omm_id="2")
    assert rfp.status == "down"
    down = Alert.objects.get(event=event, kind="rfp.down", rfp=rfp, resolved_at__isnull=True)
    assert down.severity == "critical" and down.notified
    services.sync_infrastructure(event)  # still down → no duplicate
    assert Alert.objects.filter(event=event, kind="rfp.down", rfp=rfp).count() == 1
    dect.set_rfp("2", connected=True)
    services.sync_infrastructure(event)
    down.refresh_from_db()
    assert down.resolved_at is not None
    up = Alert.objects.get(event=event, kind="rfp.up", rfp=rfp)
    assert up.severity == "info" and up.resolved_at is not None


def test_sync_omm_unreachable(event):
    fake = mock.Mock()
    fake.health.return_value = {"ok": False, "error": "connection refused"}
    with mock.patch("apps.dect.services.get_dect", return_value=fake):
        res = services.sync_infrastructure(event)
    assert res["ok"] is False
    a = Alert.objects.get(event=event, kind="omm.unreachable")
    assert a.severity == "critical" and "connection refused" in a.message
    fake.health.return_value = {"ok": True}
    fake.list_rfps.return_value = []
    fake.list_handsets.return_value = []
    with mock.patch("apps.dect.services.get_dect", return_value=fake):
        services.sync_infrastructure(event)
    a.refresh_from_db()
    assert a.resolved_at is not None


def test_sync_vanished_rfp_marked_down(dect, event):
    services.sync_infrastructure(event)
    infos = dect.list_rfps()
    with mock.patch.object(dect, "list_rfps", return_value=infos[:-1]):
        services.sync_infrastructure(event)
    gone = RFP.objects.get(event=event, omm_id="6")
    assert gone.connected is False
    assert Alert.objects.filter(kind="rfp.down", rfp=gone, resolved_at__isnull=True).exists()


def test_sync_updates_handsets(dect, event, dect_extension, dect_device):
    provision_dect_for_extension(dect_extension)
    dect.place_handset("1", "3")
    dect.subs["1"]["battery"] = 42
    services.sync_infrastructure(event)
    dect_device.refresh_from_db()
    assert dect_device.state == Device.State.SUBSCRIBED
    assert dect_device.last_seen_rfp.omm_id == "3" and dect_device.last_seen_at
    assert dect_device.battery_percent == 42 and dect_device.rssi == -60 and dect_device.handset_model == "Mitel 612d"
    assert RFP.objects.get(omm_id="3").handset_count == 1
    assert RFPStatusSample.objects.get(rfp__omm_id="3").handsets == 1
    assert services.sync_handset_positions(event) == 1


@override_settings(ALERTING={"WEBHOOK_URL": "", "NTFY_URL": "https://ntfy.example/pet", "EMAILS": "noc@example.org"})
def test_notify_alert_fans_out(event):
    rfp = RFP.objects.create(event=event, omm_id="1", name="RFP-1")
    alert = Alert.objects.create(event=event, kind="rfp.down", severity="critical", message="RFP-1 is down", rfp=rfp)
    with mock.patch("apps.dect.services.emit") as emit, mock.patch("requests.post") as post:
        services.notify_alert(alert)
    emit.assert_called_once()
    assert emit.call_args.args[0] == "dect.rfp.down"
    assert emit.call_args.args[1]["rfp"] == "RFP-1" and emit.call_args.kwargs["event"] == event
    post.assert_called_once()
    assert post.call_args.args[0] == "https://ntfy.example/pet"
    assert post.call_args.kwargs["headers"]["Priority"] == "urgent"
    assert post.call_args.kwargs["data"] == b"RFP-1 is down"
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["noc@example.org"]
    alert.refresh_from_db()
    assert alert.notified


def test_log_site_survey(dect, event, dect_extension, dect_device):
    provision_dect_for_extension(dect_extension)
    services.sync_infrastructure(event)
    dect.place_handset("1", "4")
    name = services.log_site_survey(event, "4242")
    assert name == "RFP-Camp-North"
    entry = SiteSurveyLog.objects.get()
    assert entry.device == dect_device and entry.rfp.omm_id == "4" and entry.rssi == -60
    dect_device.refresh_from_db()
    assert dect_device.last_seen_rfp.omm_id == "4"
    assert services.log_site_survey(event, "9876") == "unknown"
    assert SiteSurveyLog.objects.filter(device__isnull=True).count() == 1


def test_coverage_summary_and_weak_zones(dect, event, dect_extension, dect_device):
    provision_dect_for_extension(dect_extension)
    dect.set_rfp("5", connected=False)
    services.sync_infrastructure(event)
    cov = services.coverage_summary(event)
    assert cov["totals"]["rfps"] == 6 and cov["totals"]["down"] == 1 and cov["totals"]["handsets"] == 1
    assert {c["cluster_id"]: c["health"] for c in cov["clusters"]} == {"1": "ok", "2": "degraded"}
    reasons = {z["rfp"]: z["reason"] for z in cov["weak_zones"]}
    assert reasons["RFP-Camp-South"] == "down" and reasons["RFP-Workshop"] == "unsynced"


def test_poll_task_and_purge(dect, event):
    event.state = "live"
    event.save()
    res = poll_infrastructure()
    assert res["demo"]["ok"] and res["demo"]["rfps"] == 6
    RFPStatusSample.objects.update(at="2000-01-01T00:00:00Z")
    assert purge_old_samples(days=7) == 6


def test_poll_task_isolates_failures(dect, event):
    with mock.patch("apps.dect.services.sync_infrastructure", side_effect=RuntimeError("boom")):
        res = poll_infrastructure()
    assert res["demo"] == {"ok": False, "error": "boom"}
