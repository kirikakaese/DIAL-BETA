"""DECT integration: infrastructure monitoring, handset subscriptions, coverage map.

``get_dect(event)`` returns the adapter for the event's :class:`~apps.dect.models.DECTConnection` (the OMM
at the venue); events without one - and callers without an event - get the server-wide default
(``settings.PET_DECT_BACKEND`` + ``OMM``). Adapters accept ``config=`` (an ``OMM``-shaped dict).
"""
from __future__ import annotations

from functools import lru_cache

from django.conf import settings
from django.utils.module_loading import import_string

_per_event: dict = {}  # event pk -> ((connection pk, updated_at), adapter)


@lru_cache(maxsize=1)
def default_dect():
    return import_string(settings.PET_DECT_BACKEND)()


def get_dect(event=None):
    if event is None:
        return default_dect()
    conn = dect_connection(event)
    if conn is None:
        return default_dect()
    key = (conn.pk, conn.updated_at)
    cached = _per_event.get(event.pk)
    if cached is not None and cached[0] == key:
        return cached[1]
    adapter = import_string(conn.backend_path)(config=conn.config())
    _per_event[event.pk] = (key, adapter)
    return adapter


def dect_connection(event):
    from apps.dect.models import DECTConnection

    return DECTConnection.objects.filter(event_id=event.pk).first()


def reset_dect_cache():
    default_dect.cache_clear()
    _per_event.clear()
