"""Celery tasks: beat-driven hourly aggregation, retention purge and the Asterisk CDR table sweep."""
import logging

from celery import shared_task

log = logging.getLogger("pet.stats")


@shared_task
def aggregate_hourly():
    """Beat: rebuild HourlyStat rows (last 48h) for every live event. Returns {slug: rows_written}."""
    from . import services

    out = {}
    for event in services.live_events():
        try:
            out[event.slug] = services.aggregate_hourly(event)
        except Exception:  # noqa: BLE001 - one broken event must not stop the others
            log.exception("stats: aggregate_hourly failed for %s", event.slug)
    return out


@shared_task
def enforce_retention():
    """Beat: purge CallRecords past each event's retention. Returns {slug: deleted}."""
    from apps.events.models import Event

    from . import services

    out = {}
    for event in Event.objects.all():
        n = services.enforce_retention(event)
        if n:
            out[event.slug] = n
    return out


@shared_task
def ingest_cdrs(limit: int = 5000):
    """Sweep the Asterisk realtime ``cdr`` table for rows the ``cdr`` hook did not deliver."""
    from . import services

    out = {}
    for event in services.live_events():
        try:
            n = services.ingest_from_asterisk_table(event, limit=limit)
        except Exception:  # noqa: BLE001
            log.exception("stats: ingest_cdrs failed for %s", event.slug)
            continue
        if n:
            out[event.slug] = n
    return out
