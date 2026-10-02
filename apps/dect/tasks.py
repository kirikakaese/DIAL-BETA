"""Celery tasks: periodic infrastructure poll, single-device provisioning, sample purge."""
import logging

from celery import shared_task
from django.utils import timezone

log = logging.getLogger(__name__)


@shared_task
def poll_infrastructure():
    """Beat task (every 30 s): mirror OMM state for every event in registration/live state."""
    from . import services

    results = {}
    for event in services.polled_events():
        try:
            results[event.slug] = services.sync_infrastructure(event)
        except Exception as exc:  # noqa: BLE001 - one broken event must not stop the others
            log.exception("DECT poll failed for %s", event.slug)
            results[event.slug] = {"ok": False, "error": str(exc)}
    return results


@shared_task
def provision_device_task(device_id):
    from apps.devices.models import Device

    from .provisioning import provision_device

    try:
        device = Device.objects.select_related("event").get(pk=device_id)
    except Device.DoesNotExist:
        return None
    dev = provision_device(device)
    return dev.omm_ppn if dev is not None else None


@shared_task
def purge_old_samples(days=7):
    from .models import RFPStatusSample

    cutoff = timezone.now() - timezone.timedelta(days=days)
    deleted, _ = RFPStatusSample.objects.filter(at__lt=cutoff).delete()
    return deleted
