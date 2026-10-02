"""Celery tasks for PBX provisioning (via the durable outbox) and CDR ingestion."""
import logging

from celery import shared_task

log = logging.getLogger("pet.pbx")


# --------------------------------------------------------------------------- outbox

@shared_task(ignore_result=True)
def drain_outbox(limit: int = 100) -> dict:
    """Beat task (every 5 s): deliver due PBX jobs."""
    from apps.pbx import outbox

    return outbox.drain(limit=limit)


@shared_task(ignore_result=True)
def enqueue_and_drain(job_id: str) -> bool:
    """Immediate kick after ``enqueue`` (scheduled ``on_commit``): deliver this job if it is still due."""
    from django.utils import timezone

    from apps.pbx import outbox
    from apps.pbx.models import PBXJob

    job = PBXJob.objects.filter(pk=job_id, state__in=outbox.RETRY_STATES, next_attempt_at__lte=timezone.now()).first()
    if job is None:
        return False
    return outbox.deliver(job)


@shared_task(ignore_result=True)
def retry_dead_jobs(event_id: str | None = None) -> int:
    from apps.events.models import Event
    from apps.pbx import outbox

    event = Event.objects.filter(pk=event_id).first() if event_id else None
    return outbox.retry_dead(event)


@shared_task(ignore_result=True)
def purge_delivered_jobs(days: int = 7) -> int:
    from apps.pbx import outbox

    return outbox.purge_delivered(days=days)


# --------------------------------------------------------------------------- provisioning

@shared_task
def sync_event(event_id: str) -> int:
    """Full resync of one event (plan rows, all active extensions, all devices) via the outbox.

    Returns the number of extensions synced when the job was delivered synchronously, else 0.
    """
    from apps.events.models import Event
    from apps.pbx import outbox

    try:
        event = Event.objects.get(pk=event_id)
    except Event.DoesNotExist:
        return 0
    job = outbox.enqueue("sync_event", target=event, priority=-10)
    return job.result if job.state == job.State.DELIVERED and isinstance(job.result, int) else 0


@shared_task
def sync_device(device_id: str) -> bool:
    from apps.devices.models import Device
    from apps.pbx import outbox

    try:
        device = Device.objects.select_related("event").get(pk=device_id)
    except Device.DoesNotExist:
        return False
    job = outbox.enqueue("sync_device", target=device)
    return job.state != job.State.DEAD


@shared_task
def sync_extension(extension_id: str) -> bool:
    from apps.extensions.models import Extension
    from apps.pbx import outbox

    try:
        ext = Extension.objects.select_related("event", "owner").get(pk=extension_id)
    except Extension.DoesNotExist:
        return False
    job = outbox.enqueue("sync_extension" if ext.is_active else "remove_extension", target=ext)
    return job.state != job.State.DEAD


@shared_task
def ingest_cdrs(limit: int = 500) -> int:
    """Sweep ``cdr`` rows written by cdr_adaptive_odbc into apps.stats (fallback for the ``cdr`` hook).

    Rows are attributed to events via ``accountcode`` (= event slug, set on every endpoint).
    Not scheduled by default - add it to ``CELERY_BEAT_SCHEDULE`` if you do not use the CURL hook.
    """
    from django.utils import timezone

    from apps.events.models import Event
    from apps.pbx.api import call_service
    from apps.pbx.models import Cdr

    n = 0
    events = {e.slug[:20]: e for e in Event.objects.all()}
    for row in Cdr.objects.filter(ingested_at__isnull=True).order_by("id")[:limit]:
        event = events.get(row.accountcode or "")
        if event is None and row.dcontext and row.dcontext.startswith("pet-"):
            event = Event.objects.filter(slug=row.dcontext[4:]).first()
        if event is not None:
            call_service("apps.stats.services", "ingest_cdr", event, row.as_record())
        row.ingested_at = timezone.now()
        row.save(update_fields=["ingested_at"])
        n += 1
    return n
