"""Feature flag helpers."""
from django.conf import settings


def enabled(name: str, event=None) -> bool:
    """A feature is enabled if the deployment flag is on and, if an event is
    given, the event has not disabled it."""
    if not settings.PET_FEATURES.get(name, False):
        return False
    if event is not None:
        disabled = (event.settings or {}).get("disabled_features", [])
        return name not in disabled
    return True


def require(name: str):
    """View decorator: 404 when the feature is off."""
    from functools import wraps

    from django.http import Http404

    def deco(fn):
        @wraps(fn)
        def wrapper(request, *args, **kwargs):
            if not enabled(name, getattr(request, "event", None)):
                raise Http404("Feature disabled")
            return fn(request, *args, **kwargs)

        return wrapper

    return deco
