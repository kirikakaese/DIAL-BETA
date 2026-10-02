"""URL reversing that works whether the dect UI is mounted top-level (``dect:index``, as
documented in DEVELOPING.md) or nested under the portal namespace (``portal:dect:index``, which is
what ``apps/portal/urls.py`` currently produces)."""
from django.urls import NoReverseMatch, reverse

NAMESPACES = ("dect", "portal:dect")


def dect_reverse(name: str, *, args=None, kwargs=None) -> str:
    last = None
    for ns in NAMESPACES:
        try:
            return reverse(f"{ns}:{name}", args=args or None, kwargs=kwargs or None)
        except NoReverseMatch as exc:
            last = exc
    raise last
