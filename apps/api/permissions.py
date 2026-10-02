"""Shared DRF permission classes."""
from rest_framework import permissions

from apps.events.models import Event


def event_from_request(request, view=None):
    """Resolve the event from ``?event=<slug>`` or the object being viewed."""
    slug = request.query_params.get("event") or request.data.get("event") if hasattr(request, "data") else None
    if not slug:
        return None
    return Event.objects.filter(slug=slug).first()


class HasScope(permissions.BasePermission):
    """Service accounts must carry the scope declared on the view (``required_scopes``)."""

    def has_permission(self, request, view):
        acct = getattr(request, "service_account", None)
        if acct is None:
            return True
        scopes = getattr(view, "required_scopes", None)
        if not scopes:
            return True
        if isinstance(scopes, dict):
            scopes = scopes.get(request.method.lower(), scopes.get("default", []))
        return all(acct.has_scope(s) for s in scopes)


class IsEventOrga(permissions.BasePermission):
    """Object-level: user must be orga/admin for ``obj.event`` (or ``obj`` if it's an Event)."""

    def has_object_permission(self, request, view, obj):
        event = obj if isinstance(obj, Event) else getattr(obj, "event", None)
        if event is None:
            return request.user.is_superuser
        acct = getattr(request, "service_account", None)
        if acct is not None and acct.event_id and acct.event_id != event.pk:
            return False
        return request.user.is_orga(event)


class IsOwnerOrOrga(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        if getattr(obj, "owner_id", None) == request.user.pk:
            return True
        return IsEventOrga().has_object_permission(request, view, obj)
