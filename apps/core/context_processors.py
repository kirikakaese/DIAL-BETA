from django.conf import settings

# Files whose newest mtime forms ASSET_VERSION (also the service-worker cache name, see apps.core.views).
VERSIONED_ASSETS = ("css/dial.css", "js/dial.js", "css/pwa.css", "js/pwa.js", "js/sw.js")


def _asset_version() -> str:
    """Cache-buster for the static bundle: newest mtime of VERSIONED_ASSETS, computed once per process."""
    newest = 0
    for rel in VERSIONED_ASSETS:
        for base in settings.STATICFILES_DIRS:
            path = base / rel
            if path.exists():
                newest = max(newest, int(path.stat().st_mtime))
    return str(newest or 1)


ASSET_VERSION = _asset_version()


def _event_scoped(request, event) -> bool:
    """True when the current URL belongs to ``event`` (``/e/<slug>/...``)."""
    match = getattr(request, "resolver_match", None)
    if event is None or match is None:
        return False
    return match.kwargs.get("slug") == event.slug


def dial(request):
    """Template context: feature flags, current event, event list for switcher, sidebar data."""
    events = []
    user = getattr(request, "user", None)
    authenticated = user is not None and user.is_authenticated
    if authenticated:
        from apps.events.models import Event

        events = Event.objects.visible_to(user).order_by("-start_date")[:20]
    event = getattr(request, "event", None)
    show_sidebar = _event_scoped(request, event)
    nav_pending = 0
    if show_sidebar and getattr(request, "event_is_orga", False):
        from apps.extensions.models import Extension

        nav_pending = Extension.objects.filter(event=event, state=Extension.State.REQUESTED).count()
    return {
        "DIAL_FEATURES": settings.DIAL_FEATURES,
        "DIAL_PUBLIC_URL": settings.DIAL_PUBLIC_URL,
        "current_event": event,
        "switcher_events": events,
        "show_sidebar": show_sidebar,
        "nav_pending": nav_pending,
        "ASSET_VERSION": ASSET_VERSION,
        "signup_offered": _signup_offered(),
        "DIAL_PWA_ENABLED": settings.DIAL_PWA_ENABLED,
        "DIAL_PWA_THEME_COLOR": settings.DIAL_PWA_THEME_COLOR,
    }


def _signup_offered() -> bool:
    """The header's "Sign up" button disappears in SSO-only mode (accounts are created on first OIDC login)."""
    from apps.accounts import oidc

    return oidc.password_login_allowed()
