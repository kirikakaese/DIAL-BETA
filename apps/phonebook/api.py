"""Phonebook REST API (``/api/v1/phonebook/``).

- ``GET phonebook/?event=<slug>&q=&type=``  entries (no auth needed for public events); every entry carries
  read-only ``vcard_url`` / ``card_qr_url`` (absolute, rooted at ``PET_PUBLIC_URL``) for the business card
- ``GET phonebook/export.<fmt>?event=<slug>``  fmt in csv / vcf / ldif / pdf
- ``GET phonebook/directory/?event=<slug>``  orga: remote-directory token, per-vendor XML URLs, LDAP details
- ``POST phonebook/directory/rotate/?event=<slug>``  orga: new token (locks out every configured phone)
"""
from django.http import HttpResponse
from django.urls import path
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.api.permissions import HasScope
from apps.core.audit import log as audit
from apps.core.features import enabled
from apps.events.models import Event

from . import remote, services

SCOPES = {"get": ["phonebook:read"], "default": ["phonebook:write"]}


def _event_or_error(request):
    slug = request.query_params.get("event")
    event = Event.objects.filter(slug=slug).first() if slug else None
    if event is None:
        return None, Response({"detail": "event is required"}, status=status.HTTP_400_BAD_REQUEST)
    if not enabled("phonebook", event):
        return None, Response({"detail": "phonebook disabled"}, status=status.HTTP_404_NOT_FOUND)
    acct = getattr(request, "service_account", None)
    if acct is not None:
        if acct.event_id and acct.event_id != event.pk:
            return None, Response({"detail": "token not valid for this event"}, status=status.HTTP_403_FORBIDDEN)
        if not acct.has_scope("phonebook:read"):
            return None, Response({"detail": "scope phonebook:read required"}, status=status.HTTP_403_FORBIDDEN)
    public = event.is_public and event.state != Event.State.DRAFT
    if not public:
        if not request.user.is_authenticated:
            return None, Response({"detail": "authentication required"}, status=status.HTTP_401_UNAUTHORIZED)
        if not Event.objects.visible_to(request.user).filter(pk=event.pk).exists():
            return None, Response({"detail": "not a member of this event"}, status=status.HTTP_403_FORBIDDEN)
    return event, None


@api_view(["GET"])
@permission_classes([AllowAny])
def phonebook_list(request):
    event, err = _event_or_error(request)
    if err:
        return err
    q = request.query_params.get("q")
    etype = request.query_params.get("type") or None
    data = services.as_dicts(event, q=q, type=etype)
    return Response({"event": event.slug, "count": len(data), "results": data})


phonebook_list.cls.required_scopes = SCOPES


@api_view(["GET"])
@permission_classes([AllowAny])
def phonebook_export(request, fmt):
    event, err = _event_or_error(request)
    if err:
        return err
    if fmt not in services.EXPORTERS:
        return Response({"detail": f"unknown format {fmt!r}"}, status=status.HTTP_404_NOT_FOUND)
    body, ctype, fname = services.export(event, fmt, services.entries(event, q=request.query_params.get("q")))
    resp = HttpResponse(body, content_type=ctype)
    resp["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp


phonebook_export.cls.required_scopes = SCOPES


def _orga_event_or_error(request):
    """Event from ``?event=`` / body for the orga-only directory endpoints."""
    slug = request.query_params.get("event") or request.data.get("event")
    event = Event.objects.filter(slug=slug).first() if slug else None
    if event is None:
        return None, Response({"detail": "event is required"}, status=status.HTTP_400_BAD_REQUEST)
    if not enabled("phonebook", event):
        return None, Response({"detail": "phonebook disabled"}, status=status.HTTP_404_NOT_FOUND)
    acct = getattr(request, "service_account", None)
    if acct is not None and acct.event_id and acct.event_id != event.pk:
        return None, Response({"detail": "token not valid for this event"}, status=status.HTTP_403_FORBIDDEN)
    if not request.user.is_orga(event):
        return None, Response({"detail": "orga role required"}, status=status.HTTP_403_FORBIDDEN)
    return event, None


@api_view(["GET"])
@permission_classes([IsAuthenticated, HasScope])
def phonebook_directory(request):
    event, err = _orga_event_or_error(request)
    if err:
        return err
    return Response({"event": event.slug, **remote.directory_info(event)})


phonebook_directory.cls.required_scopes = SCOPES


@api_view(["POST"])
@permission_classes([IsAuthenticated, HasScope])
def phonebook_directory_rotate(request):
    event, err = _orga_event_or_error(request)
    if err:
        return err
    obj = services.get_settings(event)
    obj.rotate_directory_token()
    audit(action="update", actor=request.user, target=obj, event=event, request=request,
          message="Phonebook directory token rotated", changes={"directory_token": ["***", "***"]})
    return Response({"event": event.slug, **remote.directory_info(event, obj)})


phonebook_directory_rotate.cls.required_scopes = SCOPES


urlpatterns = [
    path("phonebook/", phonebook_list, name="phonebook-list"),
    path("phonebook/directory/", phonebook_directory, name="phonebook-directory"),
    path("phonebook/directory/rotate/", phonebook_directory_rotate, name="phonebook-directory-rotate"),
    path("phonebook/export.<slug:fmt>", phonebook_export, name="phonebook-export"),
]
