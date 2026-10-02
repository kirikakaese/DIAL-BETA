"""Durable PBX outbox (GURU3 "WireMessage" style).

Producers call :func:`enqueue` instead of the adapter directly. Every adapter call becomes a
:class:`~apps.pbx.models.PBXJob` row that is delivered by :func:`drain` (Celery beat every few
seconds) or by the ``enqueue_and_drain`` kick scheduled ``on_commit``. Failures are retried with
exponential backoff until ``max_attempts`` and then parked as ``dead`` for an operator to retry.

``sync_*``/``remove_*`` jobs for the same target are coalesced while still open, so a burst of
edits to one extension results in a single PBX push.

Delivery mode: when ``settings.PET_PBX_OUTBOX_SYNC`` is true (default: follows
``CELERY_TASK_ALWAYS_EAGER``, i.e. dev/test/demo without a worker) the job is delivered
synchronously inside :func:`enqueue`; the row is still written, so stats/retries work the same.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.pbx import get_pbx
from apps.pbx.models import PBXJob, PBXSyncLog

log = logging.getLogger("pet.pbx.outbox")

Kind = PBXJob.Kind
State = PBXJob.State

OPEN_STATES = (State.PENDING, State.FAILED)
RETRY_STATES = (State.PENDING, State.FAILED)
COALESCED_KINDS = {Kind.SYNC_EXTENSION, Kind.REMOVE_EXTENSION, Kind.SYNC_DEVICE, Kind.REMOVE_DEVICE, Kind.SYNC_EVENT}
TARGET_TYPES = {
    Kind.SYNC_EXTENSION: "extension", Kind.REMOVE_EXTENSION: "extension", Kind.SET_MWI: "extension",
    Kind.SYNC_DEVICE: "device", Kind.REMOVE_DEVICE: "device",
    Kind.SYNC_EVENT: "event",
}
BACKOFF_BASE_SECONDS = 5
BACKOFF_MAX_SECONDS = 600
STALE_SENDING_SECONDS = 600  # a job still "sending" after this long belongs to a crashed worker
ERROR_PREFIX = "PBX: "


class TargetVanished(Exception):
    """The Extension/Device/Event a job refers to has been deleted meanwhile."""


# --------------------------------------------------------------------------- helpers

def sync_mode() -> bool:
    explicit = getattr(settings, "PET_PBX_OUTBOX_SYNC", None)
    if explicit is not None:
        return bool(explicit)
    return bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False))


def _target_type(obj) -> str:
    return type(obj).__name__.lower()


def _jsonable(value):
    """Coerce an adapter return value into something JSONField can store."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        pass
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return _jsonable(asdict(value))
    return str(value)


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min((2 ** attempts) * BACKOFF_BASE_SECONDS, BACKOFF_MAX_SECONDS))


def _resolve_target(job: PBXJob):
    """Return the model instance the job refers to, ``None`` for target-less jobs."""
    if not job.target_type or not job.target_id:
        return None
    if job.target_type == "extension":
        from apps.extensions.models import Extension

        qs = Extension.objects.select_related("event", "owner")
    elif job.target_type == "device":
        from apps.devices.models import Device

        qs = Device.objects.select_related("event")
    elif job.target_type == "event":
        from apps.events.models import Event

        qs = Event.objects.all()
    else:
        raise TargetVanished(f"unknown target type {job.target_type!r}")
    obj = qs.filter(pk=job.target_id).first()
    if obj is None:
        raise TargetVanished("target vanished")
    return obj


def _job_event(job: PBXJob, target):
    if job.event is not None:
        return job.event
    if target is None:
        return None
    return target if job.target_type == "event" else getattr(target, "event", None)


def _call_adapter(job: PBXJob, target):
    pbx = get_pbx(_job_event(job, target))
    payload = dict(job.payload or {})
    kind = job.kind
    if kind in (Kind.SYNC_EXTENSION, Kind.REMOVE_EXTENSION, Kind.SYNC_DEVICE, Kind.REMOVE_DEVICE, Kind.SYNC_EVENT):
        return getattr(pbx, kind)(target)
    if kind == Kind.SET_MWI:
        return pbx.set_mwi(target, **payload)
    if kind in (Kind.ORIGINATE, Kind.BROADCAST):
        payload.setdefault("event", job.event)
        return getattr(pbx, kind)(**payload)
    if kind == Kind.HANGUP:
        return pbx.hangup(**payload)
    if kind == Kind.CUSTOM:
        method = payload.pop("method", None)
        if not method or method.startswith("_") or not callable(getattr(pbx, method, None)):
            raise ValueError(f"custom job: unknown adapter method {method!r}")
        args = payload.pop("args", [])
        kwargs = payload.pop("kwargs", payload)
        return getattr(pbx, method)(*args, **kwargs)
    raise ValueError(f"unknown job kind {kind!r}")


def _sync_log(job: PBXJob, target, ok: bool, message: str = ""):
    """Audit failed attempts in ``PBXSyncLog`` (the Asterisk backend logs successful pushes itself)."""
    kinds = {
        Kind.SYNC_EXTENSION: PBXSyncLog.Kind.EXTENSION, Kind.SYNC_DEVICE: PBXSyncLog.Kind.DEVICE,
        Kind.SYNC_EVENT: PBXSyncLog.Kind.EVENT,
        Kind.REMOVE_EXTENSION: PBXSyncLog.Kind.REMOVE, Kind.REMOVE_DEVICE: PBXSyncLog.Kind.REMOVE,
    }
    if ok or job.kind not in kinds:
        return
    label = getattr(target, "number", None) or getattr(target, "sip_username", None) or getattr(target, "slug", None)
    label = label or f"{job.target_type}:{job.target_id}"
    if job.kind.startswith("remove_"):
        label = f"{job.target_type} {label}"
    try:
        PBXSyncLog.objects.create(event=job.event, kind=kinds[job.kind], target=str(label)[:120], ok=False,
                                  message=f"attempt {job.attempts}/{job.max_attempts}: {message}"[:4000])
    except Exception:  # noqa: BLE001 - logging must never break delivery
        log.debug("could not write PBXSyncLog", exc_info=True)


def _update_extension(job: PBXJob, ok: bool, error: str = ""):
    """Mirror the delivery outcome into ``Extension.provisioned_at`` / ``provision_error``."""
    if job.target_type != "extension" or job.kind not in (Kind.SYNC_EXTENSION, Kind.REMOVE_EXTENSION):
        return
    from apps.extensions.models import Extension

    now = timezone.now()
    qs = Extension.objects.filter(pk=job.target_id)
    if ok:
        # only clear errors we wrote ourselves; a DECT error recorded by the provisioning task stays visible
        qs.filter(Q(provision_error="") | Q(provision_error__startswith=ERROR_PREFIX)).update(provision_error="")
        if job.kind == Kind.SYNC_EXTENSION:
            qs.update(provisioned_at=now, updated_at=now)
    else:
        qs.update(provision_error=f"{ERROR_PREFIX}{error}"[:2000], updated_at=now)


# --------------------------------------------------------------------------- public API

def enqueue(kind, *, event=None, target=None, payload=None, priority=0, delay_seconds=0) -> PBXJob:
    """Queue one adapter call. Coalesces with an open job for the same ``dedupe_key``."""
    kind = str(kind)
    if kind not in Kind.values:
        raise ValueError(f"unknown PBX job kind {kind!r}")
    payload = dict(payload or {})
    target_type = target_id = ""
    if target is not None:
        target_type = _target_type(target)
        target_id = str(target.pk)
        if event is None:
            event = target if target_type == "event" else getattr(target, "event", None)
    elif kind in TARGET_TYPES:
        raise ValueError(f"{kind} needs a target")
    now = timezone.now()
    due = now + timedelta(seconds=delay_seconds) if delay_seconds else now
    dedupe_key = f"{kind}:{target_id}" if kind in COALESCED_KINDS else ""

    job = None
    if dedupe_key:
        job = PBXJob.objects.filter(dedupe_key=dedupe_key, state__in=OPEN_STATES).order_by("created_at").first()
    if job is not None:
        job.payload = payload or job.payload
        job.priority = max(job.priority, priority)
        job.next_attempt_at = min(job.next_attempt_at, due) if job.state == State.PENDING else due
        job.event = job.event or event
        job.save(update_fields=["payload", "priority", "next_attempt_at", "event"])
    else:
        job = PBXJob.objects.create(event=event, kind=kind, target_type=target_type, target_id=target_id,
                                    payload=payload, priority=priority, next_attempt_at=due,
                                    dedupe_key=dedupe_key)
    if sync_mode():
        if not delay_seconds:
            deliver(job)
        return job

    from apps.pbx.tasks import enqueue_and_drain

    job_id = str(job.pk)
    countdown = delay_seconds or None
    transaction.on_commit(lambda: enqueue_and_drain.apply_async(args=[job_id], countdown=countdown))
    return job


def deliver(job: PBXJob) -> bool:
    """Attempt delivery of one job now. Returns ``True`` when the adapter call succeeded."""
    now = timezone.now()
    job.state = State.SENDING
    job.sent_at = now
    job.save(update_fields=["state", "sent_at"])
    target = None
    try:
        target = _resolve_target(job)
        result = _call_adapter(job, target)
    except TargetVanished as exc:
        _fail(job, target, str(exc), dead=True)
        return False
    except Exception as exc:  # noqa: BLE001 - any adapter/transport error is retried
        log.warning("PBX job %s (%s) failed: %s", job.pk, job, exc)
        _fail(job, target, exc)
        return False
    job.attempts += 1
    job.result = _jsonable(result)
    job.state = State.DELIVERED
    job.delivered_at = timezone.now()
    job.last_error = ""
    job.save(update_fields=["attempts", "result", "state", "delivered_at", "last_error"])
    _update_extension(job, ok=True)
    _notify_snapshot(job, target)
    return True


def _notify_snapshot(job: PBXJob, target):
    """After a provisioning job: tell integrators (``pbx.snapshot.changed``) when the event's venue-agent
    snapshot version moved. Cheap no-op for events in shared-database mode."""
    if job.kind not in COALESCED_KINDS:
        return
    event = _job_event(job, target)
    if event is None:
        return
    from apps.pbx import snapshot

    snapshot.notify_if_changed(event)


def _fail(job: PBXJob, target, error, *, dead=False):
    job.attempts += 1
    job.last_error = str(error)[:2000] or error.__class__.__name__
    if dead or job.attempts >= job.max_attempts:
        job.state = State.DEAD
    else:
        job.state = State.FAILED
        job.next_attempt_at = timezone.now() + _backoff(job.attempts)
    job.save(update_fields=["attempts", "last_error", "state", "next_attempt_at"])
    _sync_log(job, target, ok=False, message=job.last_error)
    _update_extension(job, ok=False, error=job.last_error)
    if job.state == State.DEAD:
        log.error("PBX job %s (%s) is dead after %s attempts: %s", job.pk, job, job.attempts, job.last_error)


def _due_jobs(limit: int, now):
    qs = (PBXJob.objects.filter(state__in=RETRY_STATES, next_attempt_at__lte=now)
          .order_by("-priority", "next_attempt_at"))
    if connection.features.has_select_for_update_skip_locked:
        qs = qs.select_for_update(skip_locked=True)
    return list(qs[:limit])


def drain(limit: int = 100) -> dict:
    """Deliver all due jobs (highest priority first). Safe to run from several workers concurrently."""
    out = {"delivered": 0, "failed": 0, "dead": 0}
    now = timezone.now()
    PBXJob.objects.filter(state=State.SENDING, sent_at__lt=now - timedelta(seconds=STALE_SENDING_SECONDS)).update(
        state=State.PENDING, next_attempt_at=now)
    if connection.features.has_select_for_update_skip_locked:
        with transaction.atomic():
            jobs = _due_jobs(limit, now)
            # move them out of the due set before releasing the row locks
            PBXJob.objects.filter(pk__in=[j.pk for j in jobs]).update(state=State.SENDING, sent_at=now)
    else:
        jobs = _due_jobs(limit, now)
    for job in jobs:
        if deliver(job):
            out["delivered"] += 1
        elif job.state == State.DEAD:
            out["dead"] += 1
        else:
            out["failed"] += 1
    return out


def queue_stats(event=None) -> dict:
    """Counts per state plus queue-health numbers for the dashboard / API / CLI."""
    qs = PBXJob.objects.all()
    if event is not None:
        qs = qs.filter(event=event)
    now = timezone.now()
    counts = {s: 0 for s in State.values}
    for row in qs.values("state").annotate(n=Count("id")):
        counts[row["state"]] = row["n"]
    oldest = qs.filter(state__in=OPEN_STATES).order_by("created_at").values_list("created_at", flat=True).first()
    last = qs.filter(state=State.DELIVERED).order_by("-delivered_at").values_list("delivered_at", flat=True).first()
    return {
        "pending": counts[State.PENDING],
        "sending": counts[State.SENDING],
        "delivered": counts[State.DELIVERED],
        "failed": counts[State.FAILED],
        "dead": counts[State.DEAD],
        "open": counts[State.PENDING] + counts[State.FAILED] + counts[State.SENDING],
        "oldest_pending_age": int((now - oldest).total_seconds()) if oldest else None,
        "last_delivery_at": last,  # datetime; DRF/CLI serialise it, the template formats it
        "sync_mode": sync_mode(),
        "backend": get_pbx(event).name,
    }


def retry_dead(event=None) -> int:
    """Reset dead jobs to pending (attempts start over). Returns the number of jobs re-queued."""
    qs = PBXJob.objects.filter(state=State.DEAD)
    if event is not None:
        qs = qs.filter(event=event)
    ids = list(qs.values_list("pk", flat=True))
    if not ids:
        return 0
    n = PBXJob.objects.filter(pk__in=ids).update(state=State.PENDING, attempts=0, next_attempt_at=timezone.now(),
                                                 last_error="")
    if sync_mode():
        drain(limit=len(ids))
    else:
        from apps.pbx.tasks import drain_outbox

        transaction.on_commit(lambda: drain_outbox.delay())
    return n


def purge_delivered(days: int = 7) -> int:
    cutoff = timezone.now() - timedelta(days=days)
    return PBXJob.objects.filter(state=State.DELIVERED, delivered_at__lt=cutoff).delete()[0]


def recent_jobs(event=None, limit: int = 20) -> list[dict]:
    qs = PBXJob.objects.all()
    if event is not None:
        qs = qs.filter(event=event)
    out = []
    for j in qs.order_by("-created_at")[:limit]:
        out.append({
            "id": str(j.pk), "kind": j.kind, "target_type": j.target_type, "target_id": j.target_id,
            "state": j.state, "attempts": j.attempts, "max_attempts": j.max_attempts, "priority": j.priority,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "next_attempt_at": j.next_attempt_at.isoformat() if j.next_attempt_at else None,
            "delivered_at": j.delivered_at.isoformat() if j.delivered_at else None,
            "last_error": j.last_error[:300],
        })
    return out
