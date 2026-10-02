"""Number history: every extension that ever carried a number, plus its audit trail.

Used by the helpdesk lookup and ``GET /api/v1/extensions/history/``. Visibility: the requested event is
always included (the caller is helpdesk/orga there); other events only if the viewer is a superuser or
orga/helpdesk of that event.
"""
from __future__ import annotations

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

from apps.core.models import AuditLog
from apps.events.models import Event, EventMembership

from .models import Extension

STAFF_ROLES = ("orga", "admin", "helpdesk")


def looks_like_number(q: str) -> bool:
    q = (q or "").strip()
    return q.isdigit() and 1 <= len(q) <= 16


def visible_events(viewer, event=None):
    """Events whose number history ``viewer`` may see (``event`` is always included)."""
    if viewer is not None and viewer.is_superuser:
        return Event.objects.all()
    q = Q(pk__in=EventMembership.objects.filter(user=viewer, role__in=STAFF_ROLES).values("event"))
    if event is not None:
        q |= Q(pk=event.pk)
    return Event.objects.filter(q)


def _user(u) -> dict | None:
    if u is None:
        return None
    return {"id": str(u.pk), "username": u.username, "email": u.email}


def _event(ev) -> dict:
    return {"slug": ev.slug, "name": ev.name}


def _ext_ref(ext) -> dict | None:
    if ext is None:
        return None
    return {"id": str(ext.pk), "event": ext.event.slug, "number": ext.number, "state": ext.state}


def _extension(ext, current_event) -> dict:
    deleted_at = ext.updated_at if ext.state == Extension.State.DELETED else None
    return {
        "id": str(ext.pk),
        "event": _event(ext.event),
        "is_current_event": ext.event_id == current_event.pk,
        "number": ext.number,
        "type": ext.type,
        "type_label": str(ext.get_type_display()),
        "state": ext.state,
        "state_label": str(ext.get_state_display()),
        "owner": _user(ext.owner),
        "display_name": ext.display_name,
        "is_temporary": ext.is_temporary,
        "created_at": ext.created_at,
        "updated_at": ext.updated_at,
        "moderated_at": ext.moderated_at,
        "moderated_by": _user(ext.moderated_by),
        "moderation_note": ext.moderation_note,
        "deleted_at": deleted_at,
        "expires_at": ext.expires_at,
        "ported_from": _ext_ref(ext.ported_from),
        "ported_to": [_ext_ref(p) for p in ext.ported_to.all()],
    }


def _audit(entry, ext_by_id: dict) -> dict:
    ext = ext_by_id.get(entry.target_id)
    return {
        "id": entry.id,
        "created_at": entry.created_at,
        "actor": _user(entry.actor),
        "actor_repr": entry.actor_repr,
        "action": entry.action,
        "action_label": str(entry.get_action_display()),
        "message": entry.message,
        "changes": entry.changes,
        "event": _event(ext.event) if ext is not None else None,
        "extension": _ext_ref(ext),
    }


def number_history(event, number: str, viewer) -> dict:
    """All extensions with ``number`` (this event first, then other visible events) and their audit entries."""
    number = (number or "").strip()
    events = visible_events(viewer, event)
    exts = list(
        Extension.objects.filter(number=number, event__in=events)
        .select_related("event", "owner", "moderated_by", "ported_from__event")
        .prefetch_related("ported_to__event")
        .order_by("-created_at")
    )
    ext_by_id = {str(e.pk): e for e in exts}
    audit = []
    if exts:
        ct = ContentType.objects.get_for_model(Extension)
        audit = list(AuditLog.objects.filter(target_type=ct, target_id__in=list(ext_by_id))
                     .select_related("actor").order_by("-created_at")[:500])
    extensions = [_extension(e, event) for e in exts]
    audit_rows = [_audit(a, ext_by_id) for a in audit]
    timeline = sorted(
        [{"kind": "extension", "at": x["created_at"], **x} for x in extensions]
        + [{"kind": "audit", "at": a["created_at"], **a} for a in audit_rows],
        key=lambda item: item["at"], reverse=True,
    )
    return {
        "number": number,
        "event": _event(event),
        "extensions": extensions,
        "audit": audit_rows,
        "timeline": timeline,
        "other_events": sorted({x["event"]["slug"] for x in extensions if not x["is_current_event"]}),
    }
