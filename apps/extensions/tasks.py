"""Celery tasks for extension provisioning and housekeeping."""
import logging

from celery import shared_task
from django.utils import timezone

log = logging.getLogger("pet.extensions")


@shared_task
def provision_extension(extension_id: str):
    """Push an active extension (and its devices) to the PBX (via the outbox) and the DECT system.

    The PBX part is a durable :class:`~apps.pbx.models.PBXJob`; when it is delivered asynchronously
    ``apps.pbx.outbox.deliver`` updates ``provisioned_at`` / ``provision_error`` once it has run.
    """
    from apps.dect.provisioning import provision_dect_for_extension
    from apps.pbx import outbox

    from .models import Extension

    try:
        ext = Extension.objects.select_related("event", "owner").get(pk=extension_id)
    except Extension.DoesNotExist:
        return
    job = outbox.enqueue("sync_extension", target=ext)
    fields = set()
    if job.state == job.State.DELIVERED:
        ext.provisioned_at = timezone.now()
        ext.provision_error = ""
        fields |= {"provisioned_at", "provision_error"}
    elif job.state in (job.State.FAILED, job.State.DEAD):
        ext.provision_error = f"{outbox.ERROR_PREFIX}{job.last_error}"[:2000]
        fields.add("provision_error")
    # else: still queued - outbox.deliver() writes provisioned_at/provision_error once it has run
    try:
        provision_dect_for_extension(ext)
        if ext.provision_error and not ext.provision_error.startswith(outbox.ERROR_PREFIX):
            ext.provision_error = ""
            fields.add("provision_error")
    except Exception as exc:  # noqa: BLE001
        log.exception("DECT provisioning failed for %s", ext)
        ext.provision_error = str(exc)[:2000]
        fields.add("provision_error")
    if fields:
        ext.save(update_fields=sorted(fields) + ["updated_at"])


@shared_task
def deprovision_extension(extension_id: str):
    from apps.dect.provisioning import deprovision_dect_for_extension
    from apps.pbx import outbox

    from .models import Extension

    try:
        ext = Extension.objects.select_related("event").get(pk=extension_id)
    except Extension.DoesNotExist:
        return
    job = outbox.enqueue("remove_extension", target=ext)
    fields = set()
    if job.state in (job.State.FAILED, job.State.DEAD):
        ext.provision_error = f"{outbox.ERROR_PREFIX}{job.last_error}"[:2000]
        fields.add("provision_error")
    try:
        deprovision_dect_for_extension(ext)
    except Exception as exc:  # noqa: BLE001
        log.exception("DECT deprovisioning failed for %s", ext)
        ext.provision_error = str(exc)[:2000]
        fields.add("provision_error")
    if fields:
        ext.save(update_fields=sorted(fields))


@shared_task
def expire_temporary_extensions():
    from . import services
    from .models import Extension

    now = timezone.now()
    qs = Extension.objects.filter(
        expires_at__lte=now,
        state__in=[Extension.State.ACTIVE, Extension.State.SUSPENDED, Extension.State.REQUESTED],
    )
    n = 0
    for ext in qs:
        ext.mark_expired()
        deprovision_extension.delay(str(ext.pk))
        services._notify_waitlist(ext.event, ext.number)
        n += 1
    return n


@shared_task
def notify_waitlist(event_id: str, number: str):
    from django.conf import settings
    from django.core.mail import send_mail

    from .models import ExtensionRequest

    for req in ExtensionRequest.objects.filter(event_id=event_id, number=number, notified_at__isnull=True):
        try:
            send_mail(
                f"[PET] {number} is available again at {req.event.name}",
                f"The extension {number} you were waiting for is free. Register it now:\n"
                f"{settings.PET_PUBLIC_URL}/e/{req.event.slug}/extensions/new/?number={number}\n",
                settings.DEFAULT_FROM_EMAIL, [req.user.email], fail_silently=True,
            )
        finally:
            req.notified_at = timezone.now()
            req.save(update_fields=["notified_at"])
