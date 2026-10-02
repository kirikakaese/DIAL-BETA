"""Emergency REST API (orga): ``emergency/targets/``, ``emergency/incidents/`` (+ ``resolve``),
``POST emergency/broadcast/``; PBX hook ``POST emergency/incident-log/`` (``X-PET-PBX-Secret``)."""
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
from apps.events.models import Event, UserGroup

from . import services
from .models import BroadcastAnnouncement, EmergencyIncident, EmergencyTarget
from .services import EmergencyError

SCOPES = {"get": ["emergency:read"], "default": ["emergency:write"]}


def _orga_event(request, slug):
    event = get_object_or_404(Event, slug=slug)
    if not request.user.is_orga(event):
        raise PermissionDenied("orga role required")
    return event


class TargetSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="slug", queryset=Event.objects.all())
    dial_target = serializers.SerializerMethodField()

    class Meta:
        model = EmergencyTarget
        fields = ["id", "event", "number", "label", "destination_extension", "fallback_number", "announce_location",
                  "priority", "dial_target"]

    def get_dial_target(self, obj):
        return services.dial_target(obj)


class IncidentSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    handled_by = serializers.CharField(source="handled_by.username", read_only=True, default=None)

    class Meta:
        model = EmergencyIncident
        fields = ["id", "event", "number", "caller_number", "caller_extension", "at", "handled_by", "notes",
                  "resolved_at"]
        read_only_fields = ["id", "at", "resolved_at"]


class BroadcastSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="slug", read_only=True)

    class Meta:
        model = BroadcastAnnouncement
        fields = ["id", "event", "text", "group", "sent_by", "sent_at", "targets", "results"]


class _OrgaViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasScope]
    required_scopes = SCOPES

    def _event(self):
        slug = self.request.query_params.get("event") or self.request.data.get("event")
        if not slug:
            raise PermissionDenied("?event=<slug> is required")
        return _orga_event(self.request, slug)

    def get_queryset(self):
        return self.queryset.filter(event=self._event())


class TargetViewSet(_OrgaViewSet):
    serializer_class = TargetSerializer
    queryset = EmergencyTarget.objects.select_related("destination_extension")

    def perform_create(self, serializer):
        d = dict(serializer.validated_data)
        event = d.pop("event")
        _orga_event(self.request, event.slug)
        try:
            serializer.instance = services.create_target(event, self.request.user, d.pop("number"),
                                                         request=self.request, **d)
        except EmergencyError as exc:
            raise ValidationError({"detail": str(exc)})


class IncidentViewSet(_OrgaViewSet):
    serializer_class = IncidentSerializer
    queryset = EmergencyIncident.objects.select_related("caller_extension", "handled_by")
    http_method_names = ["get", "post", "patch", "head", "options"]

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        inc = self.get_object()
        services.resolve_incident(inc, request.user, notes=str(request.data.get("notes") or ""), request=request)
        return Response(self.get_serializer(inc).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated, HasScope])
def broadcast(request):
    """``{event, announcement, group?: slug}`` -> ring all handsets with the announcement."""
    event = _orga_event(request, request.data.get("event"))
    group = get_object_or_404(UserGroup, event=event, slug=request.data["group"]) if request.data.get("group") else None
    try:
        ba = services.broadcast_all(event, request.user, str(request.data.get("announcement") or ""), group=group,
                                    request=request)
    except EmergencyError as exc:
        raise ValidationError({"detail": str(exc)})
    if ba is None:
        return Response({"detail": "emergency disabled"}, status=status.HTTP_404_NOT_FOUND)
    return Response(BroadcastSerializer(ba).data, status=status.HTTP_201_CREATED)


broadcast.cls.required_scopes = {"default": ["emergency:write"]}


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([])
def incident_log(request):
    """PBX hook: ``{event, number, caller}`` -> log an EmergencyIncident (header ``X-PET-PBX-Secret``)."""
    from apps.pbx.api import _authorized

    if not _authorized(request):
        return Response({"detail": "invalid PBX secret"}, status=status.HTTP_401_UNAUTHORIZED)
    event = Event.objects.filter(slug=request.data.get("event")).first()
    if event is None:
        return Response({"handled": False, "detail": "unknown event"}, status=status.HTTP_400_BAD_REQUEST)
    inc = services.log_incident(event, str(request.data.get("number") or ""), str(request.data.get("caller") or ""))
    return Response({"handled": inc is not None, "incident": inc.pk if inc else None})


def register(router):
    router.register("emergency/targets", TargetViewSet, basename="emergency-target")
    router.register("emergency/incidents", IncidentViewSet, basename="emergency-incident")


urlpatterns = [
    path("emergency/broadcast/", broadcast, name="emergency-broadcast"),
    path("emergency/incident-log/", incident_log, name="emergency-incident-log"),
]
