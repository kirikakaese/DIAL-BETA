"""Conferences REST API: ``conferences/rooms/`` (+ ``participants`` / ``kick`` actions)."""
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.api.permissions import HasScope, IsOwnerOrOrga
from apps.events.models import Event
from apps.extensions.services import ExtensionError

from . import services
from .models import ConferenceParticipant, ConferenceRoom
from .services import ConferenceError

SCOPES = {"get": ["conferences:read"], "default": ["conferences:write"]}


class ParticipantSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConferenceParticipant
        fields = ["id", "channel_id", "caller_number", "joined_at", "left_at"]


class RoomSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="slug", queryset=Event.objects.all(), write_only=True)
    number = serializers.CharField(max_length=16)
    owner = serializers.CharField(source="owner.username", read_only=True, default=None)
    participants = serializers.IntegerField(source="active_participants.count", read_only=True)

    class Meta:
        model = ConferenceRoom
        fields = ["id", "event", "number", "name", "owner", "pin", "max_participants", "record", "music_on_hold",
                  "announce_join", "is_public", "participants", "created_at"]
        read_only_fields = ["id", "created_at"]
        extra_kwargs = {"pin": {"write_only": True}}


class RoomViewSet(viewsets.ModelViewSet):
    serializer_class = RoomSerializer
    permission_classes = [IsAuthenticated, HasScope, IsOwnerOrOrga]
    required_scopes = SCOPES

    def get_queryset(self):
        user = self.request.user
        qs = ConferenceRoom.objects.select_related("extension", "owner")
        slug = self.request.query_params.get("event")
        if slug:
            event = get_object_or_404(Event, slug=slug)
            qs = qs.filter(extension__event=event)
            if user.is_orga(event):
                return qs
        elif user.is_superuser:
            return qs
        return qs.filter(Q(is_public=True) | Q(owner=user))

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = dict(ser.validated_data)
        try:
            room = services.create_room(d.pop("event"), request.user, d.pop("number"), d.pop("pin", ""),
                                        request=request, **d)
        except (ConferenceError, ExtensionError) as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(self.get_serializer(room).data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        d = {k: v for k, v in serializer.validated_data.items() if k in services.ROOM_FIELDS}
        try:
            services.update_room(serializer.instance, actor=self.request.user, request=self.request, **d)
        except ConferenceError as exc:
            raise ValidationError({"detail": str(exc)})

    @action(detail=True, methods=["get"])
    def participants(self, request, pk=None):
        room = self.get_object()
        return Response(ParticipantSerializer(services.refresh_participants(room), many=True).data)

    @action(detail=True, methods=["post"])
    def kick(self, request, pk=None):
        room = self.get_object()
        if not (room.owner_id == request.user.pk or request.user.is_orga(room.event)):
            raise PermissionDenied
        p = get_object_or_404(ConferenceParticipant, pk=request.data.get("participant"), room=room)
        return Response({"kicked": services.kick(p, actor=request.user, request=request)})


def register(router):
    router.register("conferences/rooms", RoomViewSet, basename="conference-room")
