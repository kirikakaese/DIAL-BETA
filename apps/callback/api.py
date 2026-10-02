"""Callback REST API (mounted under ``/api/v1/``).

- ``callback/requests/``          list mine (or all with ``?event=<slug>`` as orga), create (web request), ``cancel``
- ``callback/scheduled-calls/``   CRUD for own scheduled/wake-up calls, ``snooze``, ``cancel``
- ``POST callback/test-ringback/``  ``{event, extension, delay?}``
- ``POST callback/result/``       PBX result hook (``X-PET-PBX-Secret``): ``{event, kind, id, result}``
"""
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.urls import path
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import (
    action,
    api_view,
    authentication_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.api.permissions import HasScope
from apps.events.models import Event
from apps.extensions.models import Extension

from . import services
from .models import CallbackRequest, ScheduledCall, TestRingback
from .services import CallbackError

SCOPES = {"get": ["callback:read"], "default": ["callback:write"]}


def _event_or_404(slug):
    return get_object_or_404(Event, slug=slug)


def _check_account_event(request, event):
    acct = getattr(request, "service_account", None)
    if acct is not None and acct.event_id and acct.event_id != event.pk:
        raise PermissionDenied("token not valid for this event")


def _visible_events(user):
    return Event.objects.all() if user.is_superuser else Event.objects.visible_to(user)


# --------------------------------------------------------------------------- serializers

class CallbackRequestSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="slug", queryset=Event.objects.all())
    requester_number = serializers.CharField(max_length=16)
    target_number = serializers.CharField(max_length=16)
    kind = serializers.ChoiceField(choices=CallbackRequest.Kind.choices, default=CallbackRequest.Kind.CCBS)

    class Meta:
        model = CallbackRequest
        fields = ["id", "event", "kind", "requester_number", "target_number", "state", "created_at", "expires_at",
                  "attempts", "last_attempt_at", "next_attempt_at", "channel_id", "note"]
        read_only_fields = ["id", "state", "created_at", "expires_at", "attempts", "last_attempt_at",
                            "next_attempt_at", "channel_id", "note"]


class ScheduledCallSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="slug", queryset=Event.objects.all())
    extension = serializers.PrimaryKeyRelatedField(queryset=Extension.objects.all())
    extension_number = serializers.CharField(source="extension.number", read_only=True)
    owner = serializers.CharField(source="owner.username", read_only=True, default=None)

    class Meta:
        model = ScheduledCall
        fields = ["id", "event", "extension", "extension_number", "owner", "scheduled_for", "repeat", "max_retries",
                  "retry_interval_minutes", "snooze_minutes", "attempts", "state", "announcement",
                  "announcement_text", "announcement_file", "last_result", "channel_id", "next_attempt_at",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "attempts", "state", "last_result", "channel_id", "next_attempt_at",
                            "created_at", "updated_at"]


class TestRingbackSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="slug", read_only=True)

    class Meta:
        model = TestRingback
        fields = ["id", "event", "extension", "caller_number", "requested_at", "delay_seconds", "state", "channel_id"]


# --------------------------------------------------------------------------- viewsets

class CallbackRequestViewSet(viewsets.ReadOnlyModelViewSet):
    """Callbacks I requested or that target my extensions; orga see everything for ``?event=``."""

    serializer_class = CallbackRequestSerializer
    permission_classes = [IsAuthenticated, HasScope]
    required_scopes = SCOPES
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        user = self.request.user
        qs = CallbackRequest.objects.select_related("event", "requester", "target").order_by("-created_at")
        state = self.request.query_params.get("state")
        if state:
            qs = qs.filter(state=state)
        slug = self.request.query_params.get("event")
        if slug:
            event = _event_or_404(slug)
            _check_account_event(self.request, event)
            qs = qs.filter(event=event)
            if user.is_orga(event):
                return qs
        elif user.is_superuser:
            return qs
        return qs.filter(Q(requester__owner=user) | Q(target__owner=user))

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        _check_account_event(request, d["event"])
        try:
            req = services.request_callback(d["event"], d["requester_number"], d["target_number"], d["kind"],
                                            via="web", user=request.user, request=request)
        except CallbackError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(self.get_serializer(req).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        req = self.get_object()
        services.cancel(req, user=request.user, request=request)
        return Response(self.get_serializer(req).data)


class ScheduledCallViewSet(viewsets.ModelViewSet):
    serializer_class = ScheduledCallSerializer
    permission_classes = [IsAuthenticated, HasScope]
    required_scopes = SCOPES

    def get_queryset(self):
        user = self.request.user
        qs = ScheduledCall.objects.select_related("event", "extension", "owner").order_by("scheduled_for")
        slug = self.request.query_params.get("event")
        if slug:
            event = _event_or_404(slug)
            _check_account_event(self.request, event)
            qs = qs.filter(event=event)
            if user.is_orga(event):
                return qs
        elif user.is_superuser:
            return qs
        return qs.filter(Q(owner=user) | Q(extension__owner=user))

    def perform_create(self, serializer):
        d = serializer.validated_data
        _check_account_event(self.request, d["event"])
        try:
            call = services.schedule_wakeup(
                d["event"], self.request.user, d["extension"], d["scheduled_for"],
                repeat=d.get("repeat", ScheduledCall.Repeat.ONCE),
                announcement=d.get("announcement", ScheduledCall.Announcement.DEFAULT),
                announcement_text=d.get("announcement_text", ""), announcement_file=d.get("announcement_file"),
                max_retries=d.get("max_retries", 3), retry_interval_minutes=d.get("retry_interval_minutes", 5),
                snooze_minutes=d.get("snooze_minutes", 9), request=self.request,
            )
        except CallbackError as exc:
            raise ValidationError({"detail": str(exc)})
        serializer.instance = call

    def perform_update(self, serializer):
        d = serializer.validated_data
        ext = d.get("extension", serializer.instance.extension)
        event = d.get("event", serializer.instance.event)
        if ext.event_id != event.pk:
            raise ValidationError({"extension": "extension does not belong to this event"})
        if not (ext.owner_id == self.request.user.pk or self.request.user.is_orga(event)):
            raise PermissionDenied
        if "scheduled_for" in d:
            d["next_attempt_at"] = d["scheduled_for"]
        serializer.save()

    def perform_destroy(self, instance):
        services.cancel_scheduled(instance, user=self.request.user, request=self.request)

    @action(detail=True, methods=["post"])
    def snooze(self, request, pk=None):
        call = self.get_object()
        minutes = request.data.get("minutes")
        try:
            services.snooze(call, int(minutes) if minutes else None, user=request.user, request=request)
        except (CallbackError, ValueError) as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(self.get_serializer(call).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        call = self.get_object()
        services.cancel_scheduled(call, user=request.user, request=request)
        return Response(self.get_serializer(call).data)


# --------------------------------------------------------------------------- function views

@api_view(["POST"])
@permission_classes([IsAuthenticated, HasScope])
def test_ringback(request):
    """``{event: slug, extension: number, delay?: seconds}`` -> ring one of my extensions back."""
    event = _event_or_404(request.data.get("event"))
    _check_account_event(request, event)
    number = str(request.data.get("extension") or "").strip()
    if not number:
        return Response({"detail": "extension is required"}, status=status.HTTP_400_BAD_REQUEST)
    ext = services.active_extension(event, number)
    if ext is None:
        return Response({"detail": "unknown or inactive extension"}, status=status.HTTP_404_NOT_FOUND)
    delay = request.data.get("delay")
    try:
        rb = services.request_test_ringback(event, ext.number, int(delay) if delay not in (None, "") else None,
                                            user=request.user, request=request)
    except CallbackError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
    except ValueError:
        return Response({"detail": "delay must be an integer"}, status=status.HTTP_400_BAD_REQUEST)
    return Response(TestRingbackSerializer(rb).data, status=status.HTTP_201_CREATED)


test_ringback.cls.required_scopes = {"default": ["callback:write"]}


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([])
def pbx_result(request):
    """PBX result hook: ``{event, kind: callback|wakeup|ringback, id, result: answered|failed}``."""
    from apps.pbx.api import _authorized

    if not _authorized(request):
        return Response({"detail": "invalid PBX secret"}, status=status.HTTP_401_UNAUTHORIZED)
    data = request.data
    event = Event.objects.filter(slug=data.get("event")).first()
    if event is None:
        return Response({"handled": False, "detail": "unknown event"}, status=status.HTTP_400_BAD_REQUEST)
    ok = services.report_result(event, str(data.get("kind") or ""), data.get("id") or data.get("pk"),
                                str(data.get("result") or "answered"))
    return Response({"handled": bool(ok)})


def register(router):
    router.register("callback/requests", CallbackRequestViewSet, basename="callback-request")
    router.register("callback/scheduled-calls", ScheduledCallViewSet, basename="callback-scheduled-call")


urlpatterns = [
    path("callback/test-ringback/", test_ringback, name="callback-test-ringback"),
    path("callback/result/", pbx_result, name="callback-result"),
]
