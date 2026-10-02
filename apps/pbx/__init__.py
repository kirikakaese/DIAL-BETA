"""PBX integration layer.

PET is one permanent service; every event brings its own PBX at the venue. ``get_pbx(event)`` returns the
adapter for that event's :class:`~apps.pbx.models.PBXConnection`; events without one - and callers
without an event - get the server-wide default adapter (``settings.PET_PBX_BACKEND`` + ``ASTERISK``).
Adapters implement :class:`apps.pbx.base.PBXAdapter` and accept ``config=`` (an ``ASTERISK``-shaped dict).
"""
from __future__ import annotations

from functools import lru_cache

from django.conf import settings
from django.utils.module_loading import import_string

_per_event: dict = {}  # event pk -> ((connection pk, updated_at), adapter)


@lru_cache(maxsize=1)
def default_pbx():
    return import_string(settings.PET_PBX_BACKEND)()


def get_pbx(event=None):
    """Adapter for ``event`` (falls back to the server default when the event has no PBX connection)."""
    if event is None:
        return default_pbx()
    conn = pbx_connection(event)
    if conn is None:
        return default_pbx()
    key = (conn.pk, conn.updated_at)
    cached = _per_event.get(event.pk)
    if cached is not None and cached[0] == key:
        return cached[1]
    adapter = import_string(conn.backend_path)(config=conn.config())
    _per_event[event.pk] = (key, adapter)
    return adapter


def pbx_connection(event):
    """The event's ``PBXConnection`` or ``None`` (one cheap query; never raises for missing rows)."""
    from apps.pbx.models import PBXConnection

    return PBXConnection.objects.filter(event_id=event.pk).select_related("event").first()


def reset_pbx_cache():
    default_pbx.cache_clear()
    _per_event.clear()
