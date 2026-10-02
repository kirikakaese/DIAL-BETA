"""Celery tasks for events: scheduled lifecycle transitions."""
import logging

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

log = logging.getLogger("dial.events")


def _advance(event, target: str, due) -> None:
    """Move ``event`` forward to ``target`` one lifecycle step at a time (draft -> registration -> live ...).

    "Go live at Y" on a draft event implicitly opens registration first; ``Event.transition`` audits every step.
    """
    from .models import LIFECYCLE_ORDER, state_index

    for state in LIFECYCLE_ORDER[state_index(event.state) + 1:state_index(target) + 1]:
        event.transition(state, actor=None,
                         message=f"Scheduled transition to {state} (scheduled for {due:%Y-%m-%d %H:%M} UTC)")


@shared_task
def apply_scheduled_transitions() -> int:
    """Apply every due ``registration_opens_at`` / ``goes_live_at`` / ``archives_at`` schedule.

    Runs every minute from beat. A due timestamp is cleared once handled (applied, failed or stale) so it is
    never re-applied; failures are logged and audited but never abort the batch.
    Returns the number of schedules applied.
    """
    from apps.core.audit import log as audit

    from .models import SCHEDULE_FIELDS, Event

    now = timezone.now()
    due = Event.objects.filter(
        Q(registration_opens_at__lte=now) | Q(goes_live_at__lte=now) | Q(archives_at__lte=now)
    ).order_by("start_date")
    applied = 0
    for event in due:
        for target, field in SCHEDULE_FIELDS.items():  # lifecycle order
            when = getattr(event, field)
            if when is None or when > now:
                continue
            if event.is_ahead(target):
                try:
                    _advance(event, target, when)
                    applied += 1
                    log.info("event %s: scheduled transition to %s applied", event.slug, target)
                except Exception as exc:  # noqa: BLE001 - one broken event must not stop the batch
                    log.exception("event %s: scheduled transition to %s failed", event.slug, target)
                    audit(action="update", actor=None, target=event, event=event,
                          message=f"Scheduled transition to {target} failed: {exc}"[:500])
            else:
                log.info("event %s: dropping stale schedule %s (already %s)", event.slug, field, event.state)
            if getattr(event, field) is not None:
                setattr(event, field, None)
                event.save(update_fields=[field, "updated_at"])
    return applied
