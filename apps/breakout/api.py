"""Breakout REST API (orga): ``breakout/trunks/``, ``breakout/rules/``, ``breakout/permissions/``,
``GET breakout/usage/?event=<slug>``."""
from django.shortcuts import get_object_or_404
from django.urls import path
from rest_framework import serializers, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.api.permissions import HasScope
from apps.events.models import Event

from . import services
from .models import BreakoutPermission, BreakoutUsage, OutboundRule, Trunk

SCOPES = {"get": ["breakout:read"], "default": ["breakout:write"]}


def _orga_event(request, slug):
    event = get_object_or_404(Event, slug=slug)
    if not request.user.is_orga(event):
        raise PermissionDenied("orga role required")
    return event


class TrunkSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="slug", queryset=Event.objects.all())

    class Meta:
        model = Trunk
        fields = ["id", "event", "name", "sip_host", "port", "transport", "auth_user", "auth_password", "from_domain",
                  "outbound_prefix", "caller_id_default", "enabled"]
        extra_kwargs = {"auth_password": {"write_only": True}}


class RuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = OutboundRule
        fields = ["id", "trunk", "name", "pattern", "allow", "per_call_max_minutes", "priority"]


class PermissionSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="slug", queryset=Event.objects.all())

    class Meta:
        model = BreakoutPermission
        fields = ["id", "event", "extension", "user_group", "allowed", "daily_minutes_limit"]

    def validate(self, d):
        if not d.get("extension") and not d.get("user_group"):
            raise serializers.ValidationError("extension or user_group is required")
        return d


class UsageSerializer(serializers.ModelSerializer):
    extension = serializers.CharField(source="extension.number")

    class Meta:
        model = BreakoutUsage
        fields = ["id", "extension", "date", "minutes", "calls"]


class _OrgaViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasScope]
    required_scopes = SCOPES
    event_lookup = "event"

    def _event(self):
        slug = self.request.query_params.get("event") or self.request.data.get("event")
        if not slug:
            raise PermissionDenied("?event=<slug> is required")
        return _orga_event(self.request, slug)

    def get_queryset(self):
        return self.queryset.filter(**{self.event_lookup: self._event()})

    def perform_create(self, serializer):
        ev = serializer.validated_data.get("event")
        if ev is not None and not self.request.user.is_orga(ev):
            raise PermissionDenied("orga role required")
        serializer.save()


class TrunkViewSet(_OrgaViewSet):
    serializer_class = TrunkSerializer
    queryset = Trunk.objects.all()

    @action(detail=True, methods=["get"])
    def pjsip(self, request, pk=None):
        return Response({"config": services.pjsip_trunk_config(self.get_object())})


class RuleViewSet(_OrgaViewSet):
    serializer_class = RuleSerializer
    queryset = OutboundRule.objects.select_related("trunk")
    event_lookup = "trunk__event"

    def perform_create(self, serializer):
        _orga_event(self.request, serializer.validated_data["trunk"].event.slug)
        serializer.save()


class PermissionViewSet(_OrgaViewSet):
    serializer_class = PermissionSerializer
    queryset = BreakoutPermission.objects.select_related("extension", "user_group")


@api_view(["GET"])
@permission_classes([IsAuthenticated, HasScope])
def usage(request):
    event = _orga_event(request, request.query_params.get("event"))
    qs = BreakoutUsage.objects.filter(event=event).select_related("extension")
    return Response(UsageSerializer(qs[:500], many=True).data)


usage.cls.required_scopes = {"get": ["breakout:read"], "default": ["breakout:read"]}


def register(router):
    router.register("breakout/trunks", TrunkViewSet, basename="breakout-trunk")
    router.register("breakout/rules", RuleViewSet, basename="breakout-rule")
    router.register("breakout/permissions", PermissionViewSet, basename="breakout-permission")


urlpatterns = [path("breakout/usage/", usage, name="breakout-usage")]
