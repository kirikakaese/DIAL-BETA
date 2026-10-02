"""Core middleware: simple IP rate limiting and current-event resolution."""
from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django.utils.deprecation import MiddlewareMixin

RATE_LIMITED_PATHS = {
    "/accounts/login/": "login",
    "/accounts/register/": "register",
    "/accounts/password/reset/": "password_reset",
    "/api/v1/availability/": "availability",
}


def client_ip(request) -> str:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "0.0.0.0")


class RateLimitMiddleware(MiddlewareMixin):
    """Fixed-window per-IP limiter for sensitive endpoints (login, register, ...)."""

    def process_request(self, request):
        if request.method not in ("POST", "PUT", "PATCH", "DELETE") and not request.path.startswith(
            "/api/v1/availability/"
        ):
            return None
        for prefix, bucket in RATE_LIMITED_PATHS.items():
            if request.path.startswith(prefix):
                limit = settings.DIAL_RATE_LIMITS.get(bucket, 60)
                key = f"rl:{bucket}:{client_ip(request)}"
                try:
                    count = cache.get_or_set(key, 0, timeout=60)
                    count = cache.incr(key)
                except Exception:  # cache backend without incr / unavailable
                    return None
                if count > limit:
                    return HttpResponse("Too many requests", status=429)
        return None


class CurrentEventMiddleware(MiddlewareMixin):
    """Resolves the user's currently selected event (session) onto ``request.event``.

    The portal lets users switch between events; API clients pass the event
    explicitly, so this is purely a UI convenience.
    """

    def process_request(self, request):
        request.event = None
        request.event_is_orga = False
        request.event_is_helpdesk = False
        slug = request.session.get("current_event") if hasattr(request, "session") else None
        if slug:
            from apps.events.models import Event

            request.event = Event.objects.filter(slug=slug).first()
        if request.event is not None and request.user.is_authenticated:
            request.event_is_orga = request.user.is_orga(request.event)
            request.event_is_helpdesk = request.user.is_helpdesk(request.event)
