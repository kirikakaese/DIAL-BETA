"""IVR REST API: ``ivr/announcements/`` and ``ivr/menus/`` (own objects; orga see all with ``?event=``)."""
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.api.permissions import HasScope, IsOwnerOrOrga
from apps.events.models import Event
from apps.extensions.services import ExtensionError

from . import services
from .models import Announcement, IVRMenu
from .services import IVRError

SCOPES = {"get": ["ivr:read"], "default": ["ivr:write"]}


class _Base(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="slug", queryset=Event.objects.all(), write_only=True)
    number = serializers.CharField(max_length=16, write_only=True)
    extension_number = serializers.CharField(source="extension.number", read_only=True)
    owner = serializers.CharField(source="extension.owner.username", read_only=True, default=None)
    dialplan = serializers.SerializerMethodField()

    def get_dialplan(self, obj):
        return services.dialplan_for(obj.extension)


class AnnouncementSerializer(_Base):
    class Meta:
        model = Announcement
        fields = ["id", "event", "number", "extension_number", "owner", "audio", "tts_text", "language", "loop",
                  "dialplan", "created_at"]
        read_only_fields = ["id", "created_at"]


class MenuSerializer(_Base):
    class Meta:
        model = IVRMenu
        fields = ["id", "event", "number", "extension_number", "owner", "prompt_audio", "prompt_tts", "language",
                  "timeout", "invalid_retries", "options", "dialplan", "created_at"]
        read_only_fields = ["id", "created_at"]


class _OwnerViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasScope, IsOwnerOrOrga]
    required_scopes = SCOPES
    model = None

    def get_queryset(self):
        user = self.request.user
        qs = self.model.objects.select_related("extension__owner", "extension__event")
        slug = self.request.query_params.get("event")
        if slug:
            event = get_object_or_404(Event, slug=slug)
            qs = qs.filter(extension__event=event)
            if user.is_orga(event):
                return qs
        elif user.is_superuser:
            return qs
        return qs.filter(Q(extension__owner=user))

    def check_object_permissions(self, request, obj):
        # IsOwnerOrOrga looks at ``owner_id``/``event`` - delegate to the extension
        super().check_object_permissions(request, obj.extension)

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            obj = self.perform_create_obj(ser.validated_data)
        except (IVRError, ExtensionError) as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(self.get_serializer(obj).data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        d = {k: v for k, v in serializer.validated_data.items() if k not in ("event", "number")}
        try:
            self.update_obj(serializer.instance, **d)
        except IVRError as exc:
            raise ValidationError({"detail": str(exc)})


class AnnouncementViewSet(_OwnerViewSet):
    serializer_class = AnnouncementSerializer
    model = Announcement

    def perform_create_obj(self, d):
        return services.create_announcement(d["event"], self.request.user, d["number"], d.get("tts_text", ""),
                                            d.get("audio"), language=d.get("language", "en"),
                                            loop=d.get("loop", False), request=self.request)

    update_obj = staticmethod(services.update_announcement)


class MenuViewSet(_OwnerViewSet):
    serializer_class = MenuSerializer
    model = IVRMenu

    def perform_create_obj(self, d):
        return services.create_menu(d["event"], self.request.user, d["number"], d.get("options") or [],
                                    prompt_tts=d.get("prompt_tts", ""), prompt_audio=d.get("prompt_audio"),
                                    language=d.get("language", "en"), timeout=d.get("timeout", 5),
                                    invalid_retries=d.get("invalid_retries", 3), request=self.request)

    update_obj = staticmethod(services.update_menu)


def register(router):
    router.register("ivr/announcements", AnnouncementViewSet, basename="ivr-announcement")
    router.register("ivr/menus", MenuViewSet, basename="ivr-menu")
