"""Durable PBX outbox: coalescing, delivery, retries/backoff, dead jobs, stats, API and producers."""
import datetime as dt

import pytest
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.pbx import get_pbx, outbox, reset_pbx_cache
from apps.pbx.models import PBXJob, PBXSyncLog

from .conftest import bind, make_device, make_extension

pytestmark = pytest.mark.django_db

ASYNC = override_settings(DIAL_PBX_OUTBOX_SYNC=False)


@pytest.fixture(autouse=True)
def fresh_pbx():
    reset_pbx_cache()
    yield get_pbx()
    reset_pbx_cache()


def boom(*a, **kw):
    raise RuntimeError("ARI down")


# --------------------------------------------------------------------------- enqueue / coalescing

@ASYNC
def test_enqueue_creates_pending_job_with_dedupe_key(event, user):
    ext = make_extension(event, "4242", owner=user)
    job = outbox.enqueue("sync_extension", target=ext)
    assert job.state == "pending" and job.attempts == 0
    assert job.target_type == "extension" and job.target_id == str(ext.pk)
    assert job.event == event and job.dedupe_key == f"sync_extension:{ext.pk}"
    assert get_pbx().extensions == {}  # nothing delivered yet


@ASYNC
def test_enqueue_coalesces_open_job_for_same_target(event, user):
    ext = make_extension(event, "4242", owner=user)
    j1 = outbox.enqueue("sync_extension", target=ext, payload={"v": 1}, delay_seconds=30)
    j2 = outbox.enqueue("sync_extension", target=ext, payload={"v": 2}, priority=5)
    assert j1.pk == j2.pk and PBXJob.objects.count() == 1
    j1.refresh_from_db()
    assert j1.payload == {"v": 2} and j1.priority == 5
    assert j1.next_attempt_at <= timezone.now()  # pulled forward
    # a different kind for the same target is a separate job
    j3 = outbox.enqueue("remove_extension", target=ext)
    assert j3.pk != j1.pk and PBXJob.objects.count() == 2
    # delivered jobs are not coalesced into
    outbox.deliver(j1)
    j4 = outbox.enqueue("sync_extension", target=ext)
    assert j4.pk != j1.pk


@ASYNC
def test_enqueue_originate_has_no_dedupe(event):
    a = outbox.enqueue("originate", event=event, payload={"destination": "4242", "caller_id": "9000"})
    b = outbox.enqueue("originate", event=event, payload={"destination": "4242", "caller_id": "9000"})
    assert a.pk != b.pk and a.dedupe_key == ""


def test_enqueue_rejects_unknown_kind_and_missing_target(event):
    with pytest.raises(ValueError):
        outbox.enqueue("frobnicate", event=event)
    with pytest.raises(ValueError):
        outbox.enqueue("sync_extension", event=event)


def test_sync_mode_defaults_to_celery_eager(event, user, settings):
    ext = make_extension(event, "4242", owner=user)
    job = outbox.enqueue("sync_extension", target=ext)
    assert job.state == "delivered" and str(ext.pk) in get_pbx().extensions
    ext.refresh_from_db()
    assert ext.provisioned_at is not None
    settings.DIAL_PBX_OUTBOX_SYNC = False
    assert outbox.sync_mode() is False


# --------------------------------------------------------------------------- deliver / drain

@ASYNC
def test_drain_delivers_and_sets_provisioned_at(event, user):
    ext = make_extension(event, "4242", owner=user)
    dev = make_device(event, "demo-aaaa", owner=user)
    bind(ext, dev)
    outbox.enqueue("sync_extension", target=ext)
    outbox.enqueue("sync_device", target=dev)
    assert outbox.drain() == {"delivered": 2, "failed": 0, "dead": 0}
    pbx = get_pbx()
    assert pbx.extensions == {str(ext.pk): "4242"} and str(dev.pk) in pbx.devices
    ext.refresh_from_db()
    assert ext.provisioned_at is not None and ext.provision_error == ""
    job = PBXJob.objects.get(kind="sync_extension")
    assert job.state == "delivered" and job.attempts == 1 and job.delivered_at is not None
    assert outbox.drain() == {"delivered": 0, "failed": 0, "dead": 0}


@ASYNC
def test_drain_respects_due_time_and_priority(event, user):
    a = make_extension(event, "4242", owner=user)
    b = make_extension(event, "4243", owner=user)
    c = make_extension(event, "4244", owner=user)
    outbox.enqueue("sync_extension", target=a, priority=0)
    outbox.enqueue("sync_extension", target=b, priority=10)
    outbox.enqueue("sync_extension", target=c, delay_seconds=3600)
    assert outbox.drain(limit=1)["delivered"] == 1
    assert list(get_pbx().extensions.values()) == ["4243"]  # highest priority first
    assert outbox.drain()["delivered"] == 1
    assert PBXJob.objects.get(target_id=str(c.pk)).state == "pending"  # not due yet


@ASYNC
def test_drain_recovers_stale_sending_jobs(event, user):
    ext = make_extension(event, "4242", owner=user)
    job = outbox.enqueue("sync_extension", target=ext)
    PBXJob.objects.filter(pk=job.pk).update(state="sending", sent_at=timezone.now() - dt.timedelta(hours=1))
    assert outbox.drain()["delivered"] == 1
    job.refresh_from_db()
    assert job.state == "delivered"


@ASYNC
def test_deliver_stores_json_result_and_event_payload(event, user):
    make_extension(event, "4242", owner=user)
    job = outbox.enqueue("sync_event", target=event)
    assert outbox.deliver(job) is True
    assert job.result == 1 and get_pbx().synced_events == ["demo"]
    job = outbox.enqueue("originate", event=event, payload={"destination": "4242", "caller_id": "9000"})
    assert outbox.deliver(job) is True and job.result == "dummy-1"
    assert get_pbx().originated[0]["event"] == "demo"
    ext = make_extension(event, "4300", owner=user)
    job = outbox.enqueue("set_mwi", target=ext, payload={"new_messages": 2, "old_messages": 1})
    assert outbox.deliver(job) and get_pbx().mwi == [("4300", 2, 1)]


@ASYNC
def test_failing_adapter_backs_off_then_dies(event, user, monkeypatch):
    ext = make_extension(event, "4242", owner=user)
    monkeypatch.setattr(get_pbx(), "sync_extension", boom)
    job = outbox.enqueue("sync_extension", target=ext, payload={})
    job.max_attempts = 3
    job.save()

    before = timezone.now()
    assert outbox.deliver(job) is False
    assert job.state == "failed" and job.attempts == 1 and "ARI down" in job.last_error
    assert dt.timedelta(seconds=8) < job.next_attempt_at - before < dt.timedelta(seconds=12)
    ext.refresh_from_db()
    assert ext.provision_error.startswith("PBX: ") and "ARI down" in ext.provision_error
    assert ext.provisioned_at is None
    assert PBXSyncLog.objects.filter(kind="extension", ok=False, target="4242").count() == 1
    # not due yet -> drain skips it
    assert outbox.drain() == {"delivered": 0, "failed": 0, "dead": 0}
    # a re-enqueue coalesces into the failed job and makes it due again
    outbox.enqueue("sync_extension", target=ext)
    assert outbox.drain() == {"delivered": 0, "failed": 1, "dead": 0}
    job.refresh_from_db()
    assert job.attempts == 2 and job.state == "failed"
    job.next_attempt_at = timezone.now()
    job.save()
    assert outbox.drain() == {"delivered": 0, "failed": 0, "dead": 1}
    job.refresh_from_db()
    assert job.state == "dead" and job.attempts == 3
    assert PBXSyncLog.objects.filter(ok=False).count() == 3
    # dead jobs are left alone by drain; a new enqueue creates a fresh job
    assert outbox.drain() == {"delivered": 0, "failed": 0, "dead": 0}
    assert outbox.enqueue("sync_extension", target=ext).pk != job.pk


@ASYNC
def test_backoff_is_capped_at_ten_minutes():
    assert outbox._backoff(1) == dt.timedelta(seconds=10)
    assert outbox._backoff(4) == dt.timedelta(seconds=80)
    assert outbox._backoff(20) == dt.timedelta(minutes=10)


@ASYNC
def test_target_vanished_marks_job_dead(event, user):
    ext = make_extension(event, "4242", owner=user)
    job = outbox.enqueue("sync_extension", target=ext)
    ext.delete()
    assert outbox.deliver(job) is False
    assert job.state == "dead" and job.last_error == "target vanished" and job.attempts == 1


@ASYNC
def test_success_clears_only_pbx_errors(event, user):
    ext = make_extension(event, "4242", owner=user, provision_error="OMM says no")
    outbox.deliver(outbox.enqueue("sync_extension", target=ext))
    ext.refresh_from_db()
    assert ext.provision_error == "OMM says no" and ext.provisioned_at is not None
    ext.provision_error = "PBX: ARI down"
    ext.save()
    outbox.deliver(outbox.enqueue("sync_extension", target=ext))
    ext.refresh_from_db()
    assert ext.provision_error == ""


# --------------------------------------------------------------------------- stats / retry / purge

@ASYNC
def test_queue_stats_and_retry_dead(event, user, monkeypatch):
    a = make_extension(event, "4242", owner=user)
    b = make_extension(event, "4243", owner=user)
    outbox.enqueue("sync_extension", target=a)
    outbox.deliver(outbox.enqueue("sync_extension", target=b))
    dead = outbox.enqueue("remove_extension", target=b)
    monkeypatch.setattr(get_pbx(), "remove_extension", boom)
    dead.max_attempts = 1
    dead.save()
    outbox.deliver(dead)
    monkeypatch.undo()

    s = outbox.queue_stats(event)
    assert s["pending"] == 1 and s["delivered"] == 1 and s["dead"] == 1 and s["failed"] == 0
    assert s["open"] == 1 and s["oldest_pending_age"] is not None and s["last_delivery_at"] is not None
    assert s["backend"] == "dummy" and s["sync_mode"] is False
    other = outbox.queue_stats()
    assert other["dead"] == 1

    assert outbox.retry_dead(event) == 1
    dead.refresh_from_db()
    assert dead.state == "pending" and dead.attempts == 0 and dead.last_error == ""
    assert outbox.retry_dead(event) == 0
    assert outbox.drain()["delivered"] == 2
    assert get_pbx().removed_extensions == [str(b.pk)]


@ASYNC
def test_purge_delivered(event, user):
    ext = make_extension(event, "4242", owner=user)
    job = outbox.enqueue("sync_extension", target=ext)
    outbox.deliver(job)
    assert outbox.purge_delivered(days=7) == 0
    PBXJob.objects.filter(pk=job.pk).update(delivered_at=timezone.now() - dt.timedelta(days=8))
    assert outbox.purge_delivered(days=7) == 1


@ASYNC
def test_recent_jobs_shape(event, user):
    ext = make_extension(event, "4242", owner=user)
    outbox.enqueue("sync_extension", target=ext)
    rows = outbox.recent_jobs(event)
    assert len(rows) == 1 and rows[0]["kind"] == "sync_extension" and rows[0]["state"] == "pending"
    assert set(rows[0]) >= {"id", "attempts", "max_attempts", "created_at", "next_attempt_at", "last_error"}


# --------------------------------------------------------------------------- tasks / producers

@ASYNC
def test_tasks_enqueue_and_drain(event, user):
    from apps.pbx import tasks

    ext = make_extension(event, "4242", owner=user)
    assert tasks.sync_extension(str(ext.pk)) is True
    job = PBXJob.objects.get()
    assert job.state == "pending"
    assert tasks.enqueue_and_drain(str(job.pk)) is True
    assert tasks.enqueue_and_drain(str(job.pk)) is False  # already delivered
    assert tasks.drain_outbox() == {"delivered": 0, "failed": 0, "dead": 0}
    assert tasks.sync_extension("00000000-0000-0000-0000-000000000000") is False
    ext.state = ext.State.SUSPENDED
    ext.save()
    tasks.sync_extension(str(ext.pk))
    assert PBXJob.objects.filter(kind="remove_extension").exists()
    assert tasks.retry_dead_jobs(str(event.pk)) == 0 and tasks.purge_delivered_jobs() == 0


def test_sync_event_task_returns_count_in_sync_mode(event, user):
    from apps.pbx import tasks

    make_extension(event, "4242", owner=user)
    make_extension(event, "4243", owner=user)
    assert tasks.sync_event(str(event.pk)) == 2
    assert PBXJob.objects.get(kind="sync_event").result == 2


def test_provision_extension_task_creates_job(event, user):
    from apps.extensions.tasks import deprovision_extension, provision_extension

    ext = make_extension(event, "4242", owner=user)
    provision_extension(str(ext.pk))
    job = PBXJob.objects.get(kind="sync_extension", target_id=str(ext.pk))
    assert job.state == "delivered"
    ext.refresh_from_db()
    assert ext.provisioned_at is not None and ext.provision_error == ""
    assert str(ext.pk) in get_pbx().extensions
    deprovision_extension(str(ext.pk))
    assert PBXJob.objects.filter(kind="remove_extension", state="delivered").exists()
    assert get_pbx().removed_extensions == [str(ext.pk)]


def test_provision_extension_task_records_pbx_failure(event, user, monkeypatch):
    from apps.extensions.tasks import provision_extension

    ext = make_extension(event, "4242", owner=user)
    monkeypatch.setattr(get_pbx(), "sync_extension", boom)
    provision_extension(str(ext.pk))
    ext.refresh_from_db()
    assert ext.provision_error == "PBX: ARI down" and ext.provisioned_at is None
    assert PBXJob.objects.get().state == "failed"


@ASYNC
def test_provision_extension_task_leaves_pending_job_alone(event, user):
    from apps.extensions.tasks import provision_extension

    ext = make_extension(event, "4242", owner=user)
    provision_extension(str(ext.pk))
    ext.refresh_from_db()
    assert ext.provisioned_at is None and ext.provision_error == ""
    assert PBXJob.objects.get().state == "pending"
    outbox.drain()
    ext.refresh_from_db()
    assert ext.provisioned_at is not None


# --------------------------------------------------------------------------- API

@pytest.fixture
def client():
    return APIClient()


@ASYNC
def test_outbox_api_for_orga(client, event, orga, user):
    ext = make_extension(event, "4242", owner=user)
    outbox.enqueue("sync_extension", target=ext)
    client.force_authenticate(orga)
    r = client.get("/api/v1/pbx/outbox/", {"event": "demo"})
    assert r.status_code == 200
    body = r.json()
    assert body["event"] == "demo" and body["stats"]["pending"] == 1 and len(body["jobs"]) == 1
    assert body["jobs"][0]["kind"] == "sync_extension"
    r = client.post("/api/v1/pbx/outbox/retry/", {"event": "demo"})
    assert r.status_code == 200 and r.json()["retried"] == 0
    assert client.get("/api/v1/pbx/outbox/", {"event": "nope"}).status_code == 404


def test_outbox_api_forbidden_for_plain_user_and_anonymous(client, event, user, member):
    client.force_authenticate(user)
    assert client.get("/api/v1/pbx/outbox/", {"event": "demo"}).status_code == 403
    assert client.post("/api/v1/pbx/outbox/retry/", {"event": "demo"}).status_code == 403
    client.force_authenticate(None)
    assert client.get("/api/v1/pbx/outbox/", {"event": "demo"}).status_code in (401, 403)


def test_outbox_api_retry_requeues_dead(client, event, orga, user, monkeypatch):
    ext = make_extension(event, "4242", owner=user)
    monkeypatch.setattr(get_pbx(), "sync_extension", boom)
    with ASYNC:
        job = outbox.enqueue("sync_extension", target=ext)
        job.max_attempts = 1
        job.save()
        outbox.deliver(job)
    monkeypatch.undo()
    assert job.state == "dead"
    client.force_authenticate(orga)
    r = client.post("/api/v1/pbx/outbox/retry/", {"event": "demo"})
    assert r.status_code == 200 and r.json()["retried"] == 1
    job.refresh_from_db()
    assert job.state == "delivered"  # sync mode drains right away
    assert r.json()["stats"]["dead"] == 0


def test_orga_dashboard_shows_queue_widget(client, event, orga, user):
    from django.test import Client

    ext = make_extension(event, "4242", owner=user)
    outbox.enqueue("sync_extension", target=ext)
    c = Client()
    c.force_login(orga)
    r = c.get(f"/e/{event.slug}/orga/")
    assert r.status_code == 200
    html = r.content.decode()
    assert 'id="pbx-queue"' in html and "/api/v1/pbx/outbox/?event=demo" in html
