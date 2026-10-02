"""Statistics REST API (mounted under ``/api/v1/``).

- ``GET stats/summary/?event=<slug>&hours=``      totals, dispositions, by_type, top extensions (helpdesk/orga)
- ``GET stats/hourly/?event=<slug>&hours=``       per-hour series
- ``GET stats/extensions/?event=<slug>&hours=``   busiest extensions
- ``GET stats/rfps/?event=<slug>&hours=``         per-RFP erlang table + heatmap
- ``GET stats/me/export/``                        GDPR export (JSON) of the calling user's call data
- ``DELETE stats/me/``                            GDPR anonymize the calling user's call records
"""
from django.shortcuts import get_object_or_404
from django.urls import path
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.api.permissions import HasScope
from apps.core.features import enabled
from apps.events.models import Event

from . import services
from .views import parse_hours

READ_SCOPES = {"get": ["stats:read"], "default": ["stats:write"]}
WRITE_SCOPES = {"default": ["stats:write"]}


def _event_for(request) -> Event:
    slug = request.query_params.get("event")
    if not slug:
        raise PermissionDenied("event is required")
    event = get_object_or_404(Event, slug=slug)
    acct = getattr(request, "service_account", None)
    if acct is not None and acct.event_id and acct.event_id != event.pk:
        raise PermissionDenied("token not valid for this event")
    if not request.user.is_helpdesk(event):
        raise PermissionDenied
    if not enabled("stats", event):
        raise PermissionDenied("statistics disabled for this event")
    return event


def _data(request) -> dict:
    event = _event_for(request)
    return services.dashboard_data(event, hours=parse_hours(request.query_params.get("hours")))


def _scoped(scopes):
    def deco(fn):
        view = api_view(["GET"])(permission_classes([IsAuthenticated, HasScope])(fn))
        view.cls.required_scopes = scopes
        return view

    return deco


@_scoped(READ_SCOPES)
def summary(request):
    d = _data(request)
    return Response({k: d[k] for k in ("event", "hours", "from", "to", "aggregate_only", "retention_days", "totals",
                                        "dispositions", "by_type", "top_extensions", "records_kept")})


@_scoped(READ_SCOPES)
def hourly(request):
    d = _data(request)
    return Response({k: d[k] for k in ("event", "hours", "from", "to", "series")})


@_scoped(READ_SCOPES)
def extensions(request):
    d = _data(request)
    return Response({k: d[k] for k in ("event", "hours", "from", "to", "aggregate_only", "top_extensions")})


@_scoped(READ_SCOPES)
def rfps(request):
    d = _data(request)
    return Response({k: d[k] for k in ("event", "hours", "from", "to", "rfps", "heatmap")})


@_scoped(READ_SCOPES)
def me_export(request):
    return Response(services.gdpr_export(request.user))


@api_view(["DELETE"])
@permission_classes([IsAuthenticated, HasScope])
def me_delete(request):
    n = services.gdpr_delete(request.user)
    return Response({"anonymized": n}, status=status.HTTP_200_OK)


me_delete.cls.required_scopes = WRITE_SCOPES


urlpatterns = [
    path("stats/summary/", summary, name="stats-summary"),
    path("stats/hourly/", hourly, name="stats-hourly"),
    path("stats/extensions/", extensions, name="stats-extensions"),
    path("stats/rfps/", rfps, name="stats-rfps"),
    path("stats/me/export/", me_export, name="stats-me-export"),
    path("stats/me/", me_delete, name="stats-me"),
]
