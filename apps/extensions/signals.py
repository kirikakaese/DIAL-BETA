"""Signal hooks for extensions (kept minimal; business logic lives in services).

The one thing handled here: when an extension leaves the live states (deleted,
expired, rejected) nobody may keep forwarding to it. ``services.delete`` already
does this explicitly; the signal catches the other paths (``mark_expired`` from
the housekeeping task, ``reject``, admin edits).
"""
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import Extension

DEAD_STATES = {Extension.State.DELETED, Extension.State.EXPIRED, Extension.State.REJECTED}


@receiver(pre_save, sender=Extension)
def _remember_previous_state(sender, instance, update_fields=None, **kwargs):
    if instance._state.adding or (update_fields is not None and "state" not in update_fields):
        instance._previous_state = None
        return
    instance._previous_state = (
        Extension.objects.filter(pk=instance.pk).values_list("state", flat=True).first()
    )


@receiver(post_save, sender=Extension)
def _clear_forwards_when_dead(sender, instance, created, update_fields=None, **kwargs):
    if created or instance.state not in DEAD_STATES:
        return
    if update_fields is not None and "state" not in update_fields:
        return
    if getattr(instance, "_previous_state", None) in DEAD_STATES:
        return  # already dead before this save - nothing new to clear
    from .services import clear_forwards_to

    clear_forwards_to(instance)
