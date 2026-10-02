"""Emergency services.

PBX contract: ``route(event, number) -> str | None`` - a dial target (``Local/<ext>@dial-<slug>`` for a
configured destination extension, or the raw fallback number) for an emergency number; ``None``
when the flag is off / no target. The route API does not tell us who is calling, so the PBX
``dial-emergency`` context should additionally ``POST`` to ``/api/v1/emergency/incident-log/``
(``{event, number, caller}``) which calls :func:`log_incident`.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.core.audit import log
from apps.core.features import enabled
from apps.events.webhooks import emit
from apps.extensions.models import ENDPOINT_TYPES, Extension
from apps.pbx import dialplan as dp
from apps.pbx import get_pbx
from apps.pbx.base import PBXError

from .models import BroadcastAnnouncement, EmergencyIncident, EmergencyTarget

logger = logging.getLogger("dial.emergency")


class EmergencyError(Exception):
    pass


def emergency_numbers(event) -> list[str]:
    from apps.extensions.services import get_plan

    return [str(n) for n in (get_plan(event).emergency_numbers or [])]


def targets_for(event):
    return EmergencyTarget.objects.filter(event=event).select_related("destination_extension")


def dial_target(target: EmergencyTarget) -> str | None:
    dest = target.destination_extension
    if dest is not None and dest.is_active:
        return f"Local/{dest.number}@{dp.event_context(target.event)}"
    return target.fallback_number or None


def route(event, number: str) -> str | None:
    if not enabled("emergency", event) or not number:
        return None
    number = str(number).strip()
    target = targets_for(event).filter(number=number).first()
    if target is None:
        return None
    return dial_target(target)


def log_incident(event, number: str, caller: str = "", *, notes: str = "") -> EmergencyIncident | None:
    if not enabled("emergency", event):
        return None
    ext = Extension.objects.filter(event=event, number=caller).active().first() if caller else None
    inc = EmergencyIncident.objects.create(event=event, number=str(number), caller_number=caller or "",
                                           caller_extension=ext, notes=notes)
    emit("emergency.triggered", {"event": event.slug, "number": inc.number, "caller": inc.caller_number,
                                 "incident_id": inc.pk, "at": inc.at.isoformat()}, event=event)
    logger.warning("EMERGENCY %s dialed %s (%s)", event.slug, number, caller or "unknown caller")
    return inc


def resolve_incident(inc: EmergencyIncident, actor, notes: str = "", request=None) -> EmergencyIncident:
    inc.resolved_at = timezone.now()
    inc.handled_by = actor
    if notes:
        inc.notes = (inc.notes + "\n" + notes).strip()
    inc.save(update_fields=["resolved_at", "handled_by", "notes", "updated_at"])
    log(action="update", actor=actor, target=inc, event=inc.event, request=request, message="Incident resolved")
    return inc


def create_target(event, actor, number: str, *, request=None, **fields) -> EmergencyTarget:
    number = str(number).strip()
    if number not in emergency_numbers(event):
        raise EmergencyError(f"{number} is not an emergency number of this plan "
                             f"({', '.join(emergency_numbers(event))})")
    dest = fields.get("destination_extension")
    if dest is not None and dest.event_id != event.pk:
        raise EmergencyError("destination extension belongs to another event")
    if dest is None and not fields.get("fallback_number"):
        raise EmergencyError("a destination extension or a fallback number is required")
    t, created = EmergencyTarget.objects.update_or_create(event=event, number=number, defaults=fields)
    log(action="create" if created else "update", actor=actor, target=t, event=event, request=request,
        message=f"Emergency target {number}")
    return t


# --------------------------------------------------------------------------- priority

def set_priority(extension: Extension, level: int, actor, request=None) -> Extension:
    level = max(0, min(int(level), 100))
    old = extension.priority
    if old != level:
        extension.priority = level
        extension.save(update_fields=["priority", "updated_at"])
        log(action="update", actor=actor, target=extension, event=extension.event, request=request,
            message="Priority changed", changes={"priority": [old, level]})
    return extension


def preempt_candidates(event):
    """Active endpoint extensions ordered by priority - what orga can bump for preemption."""
    return (Extension.objects.filter(event=event, type__in=ENDPOINT_TYPES).active()
            .select_related("owner").order_by("-priority", "number"))


# --------------------------------------------------------------------------- broadcast

def broadcast_numbers(event, group=None) -> list[str]:
    qs = Extension.objects.filter(event=event, type__in=ENDPOINT_TYPES).active()
    if group is not None:
        qs = qs.filter(owner_id__in=group.members.values_list("user_id", flat=True))
    return list(qs.values_list("number", flat=True))


def broadcast_all(event, actor, announcement: str, group=None, *, priority: int = 10,
                  request=None) -> BroadcastAnnouncement | None:
    """Ring every active handset (or group members) and play ``announcement`` (TTS text or audio path).

    Also pushes the text as a DECT message when the ``messaging`` flag is on (best effort).
    """
    if not enabled("emergency", event):
        return None
    announcement = (announcement or "").strip()
    if not announcement:
        raise EmergencyError("announcement text or audio path is required")
    numbers = broadcast_numbers(event, group)
    results: dict = {"numbers": numbers, "channels": [], "text_broadcast": None, "error": ""}
    try:
        results["channels"] = get_pbx(event).broadcast(event=event, numbers=numbers, announcement=announcement,
                                                       priority=priority)
    except PBXError as exc:
        results["error"] = str(exc)
    if enabled("messaging", event) and not announcement.startswith("/"):
        try:
            from apps.messaging.services import broadcast as text_broadcast

            bc = text_broadcast(event, actor, announcement[:480], group=group, priority="high")
            results["text_broadcast"] = getattr(bc, "pk", None)
        except Exception as exc:  # noqa: BLE001 - messaging is optional here
            logger.warning("emergency text broadcast failed: %s", exc)
            results["text_error"] = str(exc)[:200]
    ba = BroadcastAnnouncement.objects.create(event=event, text=announcement, group=group, sent_by=actor,
                                              targets=len(numbers), results=results)
    log(action="other", actor=actor, target=ba, event=event, request=request,
        message=f"Emergency broadcast to {len(numbers)} extensions")
    emit("emergency.triggered", {"event": event.slug, "kind": "broadcast", "broadcast_id": ba.pk,
                                 "targets": len(numbers), "text": announcement[:200]}, event=event)
    return ba
