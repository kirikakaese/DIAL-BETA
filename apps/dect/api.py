"""DECT REST API: read-only infrastructure views plus sync trigger and coverage summary.

Mounted under ``/api/v1/dect/...``. Visible to orga/helpdesk members of the event (superusers see
everything); service accounts additionally need ``dect:read`` / ``dect:write`` scopes.
"""
from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.urls import path
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.api.permissions import HasScope
from apps.devices.models import Device
from apps.events.models import Event

from . import services
from .models import RFP, Alert, SyncCluster

STAFF_ROLES = ("orga", "admin", "helpdesk")


def _staff_events(user):
    if user.is_superuser:
        return Event.objects.all()
    return Event.objects.filter(memberships__user=user, memberships__role__in=STAFF_ROLES)


def _event_for(request, *, orga=False):
    ev = get_object_or_404(Event, slug=request.query_params.get("event") or request.data.get("event"))
    ok = request.user.is_orga(ev) if orga else request.user.is_helpdesk(ev)
    acct = getattr(request, "service_account", None)
    if acct is not None and acct.event_id and acct.event_id != ev.pk:
        ok = False
    if not ok:
        raise PermissionDenied
    return ev


# --- serializers ------------------------------------------------------------

class SyncClusterSerializer(serializers.ModelSerializer):
    health = serializers.CharField(read_only=True)
    event = serializers.SlugRelatedField(slug_field="slug", read_only=True)

    class Meta:
        model = SyncCluster
        fields = ["id", "event", "cluster_id", "name", "health"]


class RFPSerializer(serializers.ModelSerializer):
    status = serializers.CharField(read_only=True)
    cluster = serializers.SlugRelatedField(slug_field="cluster_id", read_only=True)
    event = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    handsets = serializers.IntegerField(source="n_handsets", read_only=True, default=0)

    class Meta:
        model = RFP
        fields = ["id", "event", "omm_id", "name", "mac_address", "ip_address", "location", "cluster", "sync_source",
                  "is_active", "connected", "synced", "status", "last_state_change", "last_seen_at", "active_calls",
                  "venue_map", "pos_x", "pos_y", "handsets"]


class AlertSerializer(serializers.ModelSerializer):
    rfp_name = serializers.CharField(source="rfp.name", read_only=True, default=None)
    event = serializers.SlugRelatedField(slug_field="slug", read_only=True)

    class Meta:
        model = Alert
        fields = ["id", "event", "severity", "kind", "rfp", "rfp_name", "message", "created_at", "resolved_at",
                  "notified"]


class HandsetSerializer(serializers.ModelSerializer):
    owner = serializers.CharField(source="owner.username", read_only=True, default=None)
    event = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    last_seen_rfp = serializers.CharField(source="last_seen_rfp.name", read_only=True, default=None)
    extensions = serializers.SerializerMethodField()

    class Meta:
        model = Device
        fields = ["id", "event", "owner", "name", "ipei", "state", "omm_ppn", "handset_model", "last_seen_rfp",
                  "last_seen_at", "battery_percent", "rssi", "extensions"]

    def get_extensions(self, obj):
        return [b.extension.number for b in obj.bindings.all() if b.is_active]


# --- viewsets ---------------------------------------------------------------

class _EventScopedReadOnly(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, HasScope]
    required_scopes = {"get": ["dect:read"], "default": ["dect:write"]}
    filterset_fields = ["event__slug"]
    event_field = "event"

    def base_queryset(self):
        raise NotImplementedError

    def get_queryset(self):
        qs = self.base_queryset().filter(**{f"{self.event_field}__in": _staff_events(self.request.user)})
        acct = getattr(self.request, "service_account", None)
        if acct is not None and acct.event_id:
            qs = qs.filter(**{f"{self.event_field}_id": acct.event_id})
        return qs


class RFPViewSet(_EventScopedReadOnly):
    serializer_class = RFPSerializer
    filterset_fields = ["event__slug", "connected", "synced", "is_active", "cluster__cluster_id"]
    search_fields = ["name", "omm_id", "location", "mac_address"]

    def base_queryset(self):
        return RFP.objects.select_related("cluster", "event").annotate(n_handsets=Count("handsets"))


class SyncClusterViewSet(_EventScopedReadOnly):
    serializer_class = SyncClusterSerializer

    def base_queryset(self):
        return SyncCluster.objects.select_related("event").prefetch_related("rfps")


class AlertViewSet(_EventScopedReadOnly):
    serializer_class = AlertSerializer
    filterset_fields = ["event__slug", "severity", "kind"]

    def base_queryset(self):
        qs = Alert.objects.select_related("rfp", "event")
        if self.request.query_params.get("open") in ("1", "true"):
            qs = qs.filter(resolved_at__isnull=True)
        return qs


class HandsetViewSet(_EventScopedReadOnly):
    serializer_class = HandsetSerializer
    filterset_fields = ["event__slug", "state"]
    search_fields = ["ipei", "name", "owner__username", "bindings__extension__number"]

    def base_queryset(self):
        return (Device.objects.filter(type="dect").select_related("owner", "last_seen_rfp", "event")
                .prefetch_related("bindings__extension"))


# --- function views -----------------------------------------------------------

@extend_schema(parameters=[OpenApiParameter("event", str, required=True)], responses={200: dict})
@api_view(["POST"])
@permission_classes([IsAuthenticated, HasScope])
def sync(request):
    """Trigger an immediate OMM sync for one event (orga only)."""
    ev = _event_for(request, orga=True)
    result = services.sync_infrastructure(ev)
    return Response(result, status=status.HTTP_200_OK if result.get("ok") else status.HTTP_502_BAD_GATEWAY)


sync.cls.required_scopes = {"default": ["dect:write"]}


@extend_schema(parameters=[OpenApiParameter("event", str, required=True)], responses={200: dict})
@api_view(["GET"])
@permission_classes([IsAuthenticated, HasScope])
def coverage(request):
    """Per-RFP handset counts, per-cluster health, totals and weak zones (orga/helpdesk)."""
    ev = _event_for(request)
    return Response(services.coverage_summary(ev))


coverage.cls.required_scopes = {"get": ["dect:read"]}


def register(router):
    router.register("dect/rfps", RFPViewSet, basename="dect-rfp")
    router.register("dect/clusters", SyncClusterViewSet, basename="dect-cluster")
    router.register("dect/alerts", AlertViewSet, basename="dect-alert")
    router.register("dect/handsets", HandsetViewSet, basename="dect-handset")


urlpatterns = [
    path("dect/sync/", sync, name="dect-sync"),
    path("dect/coverage/", coverage, name="dect-coverage"),
]
