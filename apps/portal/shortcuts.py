"""Small helpers shared by portal views across apps."""
from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from apps.events.models import Event


def get_event_or_404(request, slug: str) -> Event:
    """Resolve an event the current user may see and mark it current in the session."""
    event = get_object_or_404(Event.objects.visible_to(request.user), slug=slug)
    if hasattr(request, "session") and request.session.get("current_event") != slug:
        request.session["current_event"] = slug
    request.event = event
    if request.user.is_authenticated:
        request.event_is_orga = request.user.is_orga(event)
        request.event_is_helpdesk = request.user.is_helpdesk(event)
    return event


def require_orga(view):
    """Decorator for views with a ``slug`` kwarg: user must be orga/admin of that event.

    The resolved event is passed as ``event`` kwarg.
    """

    @wraps(view)
    def wrapper(request, slug, *args, **kwargs):
        event = get_event_or_404(request, slug)
        if not request.user.is_authenticated or not request.user.is_orga(event):
            raise PermissionDenied
        return view(request, slug, *args, event=event, **kwargs)

    return wrapper


def require_helpdesk(view):
    @wraps(view)
    def wrapper(request, slug, *args, **kwargs):
        event = get_event_or_404(request, slug)
        if not request.user.is_authenticated or not request.user.is_helpdesk(event):
            raise PermissionDenied
        return view(request, slug, *args, event=event, **kwargs)

    return wrapper


def with_event(view):
    """Decorator: resolve event and pass it as ``event`` kwarg (any visible user)."""

    @wraps(view)
    def wrapper(request, slug, *args, **kwargs):
        event = get_event_or_404(request, slug)
        return view(request, slug, *args, event=event, **kwargs)

    return wrapper
