"""Audit logging helper used across all apps.

Usage::

    from apps.core.audit import log
    log(actor=request.user, action="approve", target=extension, event=extension.event,
        message="Approved vanity number", changes={"state": ["requested", "active"]})
"""
from __future__ import annotations

from typing import Any

from django.contrib.contenttypes.models import ContentType

from .models import AuditLog


def log(
    *,
    action: str,
    actor=None,
    target=None,
    event=None,
    message: str = "",
    changes: dict[str, Any] | None = None,
    request=None,
) -> AuditLog:
    if actor is None and request is not None and getattr(request, "user", None) is not None:
        actor = request.user if request.user.is_authenticated else None
    if event is None and target is not None:
        event = getattr(target, "event", None)
    ip = None
    if request is not None:
        ip = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip() or request.META.get(
            "REMOTE_ADDR"
        )
    entry = AuditLog(
        actor=actor,
        actor_repr=str(actor) if actor else "system",
        event=event,
        action=action,
        message=message[:500],
        changes=changes or {},
        ip_address=ip or None,
    )
    if target is not None and getattr(target, "pk", None) is not None:
        entry.target_type = ContentType.objects.get_for_model(target)
        entry.target_id = str(target.pk)
        entry.target_repr = str(target)[:300]
    entry.save()
    return entry
