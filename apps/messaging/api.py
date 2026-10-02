"""Messaging REST API: ``messaging/messages/`` (list mine, create) and ``POST messaging/broadcast/``."""
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.urls import path
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.api.permissions import HasScope
from apps.core.features import enabled
from apps.events.models import Event, UserGroup
from apps.extensions.models import Extension

from . import services
from .models import Broadcast, Message
from .services import MessagingError

SCOPES = {"get": ["messaging:read"], "default": ["messaging:write"]}


def _event(slug):
    return get_object_or_404(Event, slug=slug)


class MessageSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="slug", queryset=Event.objects.all())
    recipient_number = serializers.CharField(max_length=16, write_only=True)
    recipient = serializers.CharField(source="recipient_extension.number", read_only=True, default=None)
    sender = serializers.CharField(source="sender.username", read_only=True, default=None)

    class Meta:
        model = Message
        fields = ["id", "event", "direction", "sender", "recipient", "recipient_number", "text", "state", "sent_at",
                  "error", "created_at"]
        read_only_fields = ["id", "direction", "state", "sent_at", "error", "created_at"]


class MessageViewSet(viewsets.ModelViewSet):
    serializer_class = MessageSerializer
    permission_classes = [IsAuthenticated, HasScope]
    required_scopes = SCOPES
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        user = self.request.user
        qs = Message.objects.select_related("event", "sender", "recipient_extension", "sender_extension")
        slug = self.request.query_params.get("event")
        if slug:
            event = _event(slug)
            qs = qs.filter(event=event)
            if user.is_orga(event):
                return qs
        return qs.filter(Q(sender=user) | Q(recipient_extension__owner=user))

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        if not enabled("messaging", d["event"]):
            raise PermissionDenied("messaging disabled")
        ext = Extension.objects.filter(event=d["event"], number=d["recipient_number"]).active().first()
        if ext is None:
            raise ValidationError({"recipient_number": "unknown or inactive extension"})
        try:
            msg = services.send_to_extension(d["event"], request.user, ext, d["text"])
        except MessagingError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(self.get_serializer(msg).data, status=status.HTTP_201_CREATED)


class BroadcastSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    group = serializers.SlugRelatedField(slug_field="slug", read_only=True)

    class Meta:
        model = Broadcast
        fields = ["id", "event", "target", "group", "text", "sent_count", "failed_count", "created_at"]


@api_view(["POST"])
@permission_classes([IsAuthenticated, HasScope])
def broadcast(request):
    """``{event: slug, text, group?: slug}`` -> orga text broadcast."""
    event = _event(request.data.get("event"))
    if not request.user.is_orga(event):
        raise PermissionDenied("orga role required")
    group = None
    if request.data.get("group"):
        group = get_object_or_404(UserGroup, event=event, slug=request.data["group"])
    try:
        bc = services.broadcast(event, request.user, str(request.data.get("text") or ""), group=group,
                                request=request)
    except MessagingError as exc:
        raise ValidationError({"detail": str(exc)})
    if bc is None:
        return Response({"detail": "messaging disabled"}, status=status.HTTP_404_NOT_FOUND)
    return Response(BroadcastSerializer(bc).data, status=status.HTTP_201_CREATED)


broadcast.cls.required_scopes = {"default": ["messaging:write"]}


def register(router):
    router.register("messaging/messages", MessageViewSet, basename="messaging-message")


urlpatterns = [path("messaging/broadcast/", broadcast, name="messaging-broadcast")]
