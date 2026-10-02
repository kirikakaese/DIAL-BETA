"""Info pages REST API (``/api/v1/pages/``).

- ``pages/?event=<slug>``   list (``event`` required); unpublished pages only for orga
- ``pages/{id}/``           retrieve / update / delete
- ``pages/``                create ``{event, slug, title, body, order?, published?, show_on_dashboard?}``

Reading needs membership of (or visibility of) the event, writing the orga role. ``body_html`` is the
rendered (escaped) Markdown.
"""
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import serializers, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated

from apps.api.permissions import HasScope, IsEventOrga
from apps.events.models import Event

from .models import InfoPage
from .views import notify

SCOPES = {"get": ["pages:read"], "default": ["pages:write"]}


def _check_account_event(request, event):
    acct = getattr(request, "service_account", None)
    if acct is not None and acct.event_id and acct.event_id != event.pk:
        raise PermissionDenied("token not valid for this event")


class InfoPageSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="slug", queryset=Event.objects.all())
    body_html = serializers.CharField(read_only=True)

    class Meta:
        model = InfoPage
        fields = ["id", "event", "slug", "title", "body", "body_html", "order", "published", "show_on_dashboard",
                  "updated_at"]
        read_only_fields = ["id", "body_html", "updated_at"]
        validators = []  # (event, slug) uniqueness is reported on the ``slug`` field in validate()

    def validate(self, attrs):
        event = attrs.get("event") or (self.instance.event if self.instance else None)
        slug = attrs.get("slug") or (self.instance.slug if self.instance else None)
        if event is not None and slug is not None:
            if slug in ("manage", "new"):
                raise ValidationError({"slug": "this slug is reserved"})
            qs = InfoPage.objects.filter(event=event, slug=slug)
            if self.instance is not None:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationError({"slug": "a page with this slug already exists in this event"})
        return attrs


class InfoPageViewSet(viewsets.ModelViewSet):
    serializer_class = InfoPageSerializer
    permission_classes = [IsAuthenticated, HasScope, IsEventOrga]
    required_scopes = SCOPES

    def get_queryset(self):
        user = self.request.user
        qs = InfoPage.objects.select_related("event")
        if not user.is_superuser:
            qs = qs.filter(event__in=Event.objects.visible_to(user))
        slug = self.request.query_params.get("event")
        if slug:
            event = get_object_or_404(Event, slug=slug)
            _check_account_event(self.request, event)
            qs = qs.filter(event=event)
            if not user.is_orga(event):
                qs = qs.filter(published=True)
        elif self.action == "list":
            raise ValidationError({"event": "pass ?event=<slug>"})
        elif not user.is_superuser:
            # detail routes without ?event=: unpublished pages only for orga/admin of that event
            orga_of = Event.objects.filter(memberships__user=user, memberships__role__in=("orga", "admin"))
            qs = qs.filter(Q(published=True) | Q(event__in=orga_of))
        return qs

    def check_object_permissions(self, request, obj):
        # reading is allowed for everyone who can see the page; IsEventOrga only guards writes
        if request.method in ("GET", "HEAD", "OPTIONS"):
            _check_account_event(request, obj.event)
            return
        super().check_object_permissions(request, obj)

    def perform_create(self, serializer):
        event = serializer.validated_data["event"]
        _check_account_event(self.request, event)
        if not self.request.user.is_orga(event):
            raise PermissionDenied("orga role required")
        page = serializer.save(updated_by=self.request.user)
        notify(page, "create", self.request.user, self.request)

    def perform_update(self, serializer):
        if "event" in serializer.validated_data and serializer.validated_data["event"] != serializer.instance.event:
            raise ValidationError({"event": "a page cannot be moved to another event"})
        changed = sorted(k for k in serializer.validated_data if k != "event")
        page = serializer.save(updated_by=self.request.user)
        notify(page, "update", self.request.user, self.request, changes={k: ["…", "…"] for k in changed})

    def perform_destroy(self, instance):
        notify(instance, "delete", self.request.user, self.request)
        instance.delete()


def register(router):
    router.register("pages", InfoPageViewSet, basename="page")
