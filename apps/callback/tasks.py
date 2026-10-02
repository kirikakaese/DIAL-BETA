"""Celery tasks: beat-driven dispatch/expiry plus one-shot fire tasks."""
import logging

from celery import shared_task

log = logging.getLogger("dial.callback")


@shared_task
def dispatch_due_callbacks():
    """Beat: fire due wake-up calls, time out unanswered ones and retry failed callback originates."""
    from . import services

    return services.dispatch_due()


@shared_task
def expire_stale_requests():
    """Beat: pending callback requests past ``expires_at`` -> ``expired``."""
    from . import services

    return services.expire_stale()


@shared_task
def fire_test_ringback(pk: int):
    from . import services
    from .models import TestRingback

    rb = TestRingback.objects.filter(pk=pk).select_related("event").first()
    if rb is None:
        return False
    return services.fire_test_ringback(rb)


@shared_task
def fire_scheduled_call(pk: int):
    from . import services
    from .models import ScheduledCall

    call = ScheduledCall.objects.filter(pk=pk).select_related("event", "extension").first()
    if call is None:
        return False
    return services.fire_scheduled_call(call)
