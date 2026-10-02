"""Voicemail REST API (``/api/v1/voicemail/``).

- ``voicemail/mailboxes/``   my mailboxes (``?event=<slug>``; orga see all of an event), ``PATCH`` settings
- ``voicemail/messages/``    my messages (``?event=&mailbox=&unread=1``), ``POST {id}/mark-read/``,
                             ``POST {id}/mark-unread/``, ``DELETE {id}/``, ``GET {id}/audio/``
"""
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.api.permissions import HasScope
from apps.events.models import Event

from . import services
from .models import Mailbox, Message
from .services import VoicemailError

SCOPES = {"get": ["voicemail:read"], "default": ["voicemail:write"]}


def _check_account_event(request, event):
    acct = getattr(request, "service_account", None)
    if acct is not None and acct.event_id and acct.event_id != event.pk:
        raise PermissionDenied("token not valid for this event")


class MailboxSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    number = serializers.CharField(source="extension.number", read_only=True)
    extension = serializers.PrimaryKeyRelatedField(read_only=True)
    unread = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()
    has_greeting = serializers.SerializerMethodField()

    class Meta:
        model = Mailbox
        fields = ["id", "event", "extension", "number", "pin", "email_delivery", "email", "max_messages", "enabled",
                  "unread", "total", "has_greeting", "updated_at"]
        read_only_fields = ["id", "updated_at"]
        extra_kwargs = {"pin": {"write_only": True}}

    def get_unread(self, obj):
        return obj.messages.filter(is_read=False).count()

    def get_total(self, obj):
        return obj.messages.count()

    def get_has_greeting(self, obj):
        return bool(obj.greeting)


class MessageSerializer(serializers.ModelSerializer):
    mailbox_number = serializers.CharField(source="mailbox.extension.number", read_only=True)
    has_audio = serializers.BooleanField(read_only=True)
    audio_url = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = ["id", "mailbox", "mailbox_number", "caller_number", "caller_name", "received_at", "duration_seconds",
                  "is_read", "transcript", "has_audio", "audio_url"]
        read_only_fields = fields

    def get_audio_url(self, obj):
        if not obj.has_audio:
            return None
        request = self.context.get("request")
        path = f"/api/v1/voicemail/messages/{obj.pk}/audio/"
        return request.build_absolute_uri(path) if request else path


def _scope_event(request, qs, field="event"):
    slug = request.query_params.get("event")
    if slug:
        event = get_object_or_404(Event, slug=slug)
        _check_account_event(request, event)
        qs = qs.filter(**{field: event})
        if request.user.is_orga(event):
            return qs, True
    elif request.user.is_superuser:
        return qs, True
    return qs, False


class MailboxViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin,
                     viewsets.GenericViewSet):
    serializer_class = MailboxSerializer
    permission_classes = [IsAuthenticated, HasScope]
    required_scopes = SCOPES
    http_method_names = ["get", "patch", "put", "head", "options"]

    def get_queryset(self):
        qs = Mailbox.objects.select_related("extension__owner", "event").exclude(
            extension__state__in=["deleted", "rejected"])
        qs, all_visible = _scope_event(self.request, qs)
        if all_visible:
            return qs
        return qs.filter(extension__owner=self.request.user)

    def list(self, request, *args, **kwargs):
        slug = request.query_params.get("event")
        if slug:  # make sure my mailboxes exist before listing
            event = get_object_or_404(Event, slug=slug)
            services.mailboxes_for(request.user, event)
        return super().list(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        mailbox = self.get_object()
        if not services.can_access(request.user, mailbox):
            raise PermissionDenied
        ser = self.get_serializer(mailbox, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        try:
            services.update_mailbox(Mailbox.objects.get(pk=mailbox.pk), actor=request.user, request=request,
                                    **ser.validated_data)
        except VoicemailError as exc:
            raise ValidationError({"pin": str(exc)})
        mailbox.refresh_from_db()
        return Response(self.get_serializer(mailbox).data)


class MessageViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.DestroyModelMixin,
                     viewsets.GenericViewSet):
    serializer_class = MessageSerializer
    permission_classes = [IsAuthenticated, HasScope]
    required_scopes = SCOPES

    def get_queryset(self):
        qs = Message.objects.select_related("mailbox__extension__owner", "mailbox__event").order_by("-received_at")
        # messages are private: orga do NOT get to read them, only the owner does
        slug = self.request.query_params.get("event")
        if slug:
            event = get_object_or_404(Event, slug=slug)
            _check_account_event(self.request, event)
            qs = qs.filter(mailbox__event=event)
        qs = qs.filter(mailbox__extension__owner=self.request.user)
        mb = self.request.query_params.get("mailbox")
        if mb:
            qs = qs.filter(mailbox__extension__number=mb)
        if self.request.query_params.get("unread"):
            qs = qs.filter(is_read=False)
        return qs

    def perform_destroy(self, instance):
        services.delete_message(instance, actor=self.request.user, request=self.request)

    @action(detail=True, methods=["post"], url_path="mark-read")
    def mark_read(self, request, pk=None):
        msg = services.mark_read(self.get_object(), True)
        return Response(self.get_serializer(msg).data)

    @action(detail=True, methods=["post"], url_path="mark-unread")
    def mark_unread(self, request, pk=None):
        msg = services.mark_read(self.get_object(), False)
        return Response(self.get_serializer(msg).data)

    @action(detail=True, methods=["get"])
    def audio(self, request, pk=None):
        msg = self.get_object()
        if not msg.has_audio:
            raise Http404("no audio")
        resp = FileResponse(msg.audio.open("rb"), content_type=msg.content_type)
        resp["Cache-Control"] = "private, no-store"
        return resp


def register(router):
    router.register("voicemail/mailboxes", MailboxViewSet, basename="voicemail-mailbox")
    router.register("voicemail/messages", MessageViewSet, basename="voicemail-message")
