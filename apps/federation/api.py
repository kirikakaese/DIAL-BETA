"""Federation REST API: ``federation/peers/`` (orga) and public ``GET federation/directory/``."""
from django.shortcuts import get_object_or_404
from django.urls import path
from rest_framework import serializers, viewsets
from rest_framework.decorators import action, api_view, authentication_classes, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.api.permissions import HasScope, IsEventOrga
from apps.core.features import enabled
from apps.events.models import Event

from . import services
from .models import FederationPeer
from .services import FederationError

SCOPES = {"get": ["federation:read"], "default": ["federation:write"]}


class PeerSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="slug", queryset=Event.objects.all())
    endpoint_name = serializers.CharField(read_only=True)

    class Meta:
        model = FederationPeer
        fields = ["id", "event", "name", "remote_prefix", "sip_host", "sip_port", "transport", "srtp", "auth_user",
                  "auth_password", "remote_event_name", "directory_url", "state", "last_seen", "endpoint_name",
                  "created_at"]
        read_only_fields = ["id", "last_seen", "created_at"]
        extra_kwargs = {"auth_password": {"write_only": True}}


class PeerViewSet(viewsets.ModelViewSet):
    serializer_class = PeerSerializer
    permission_classes = [IsAuthenticated, HasScope, IsEventOrga]
    required_scopes = SCOPES

    def get_queryset(self):
        user = self.request.user
        qs = FederationPeer.objects.select_related("event")
        slug = self.request.query_params.get("event")
        if slug:
            event = get_object_or_404(Event, slug=slug)
            if not user.is_orga(event):
                raise PermissionDenied("orga role required")
            return qs.filter(event=event)
        if user.is_superuser:
            return qs
        return qs.filter(event__memberships__user=user, event__memberships__role__in=("orga", "admin"))

    def perform_create(self, serializer):
        d = dict(serializer.validated_data)
        event = d.pop("event")
        if not self.request.user.is_orga(event):
            raise PermissionDenied("orga role required")
        try:
            serializer.instance = services.register_peer(event, self.request.user, request=self.request, **d)
        except FederationError as exc:
            raise ValidationError({"detail": str(exc)})

    def perform_update(self, serializer):
        d = {k: v for k, v in serializer.validated_data.items() if k != "event"}
        services.update_peer(serializer.instance, actor=self.request.user, request=self.request, **d)

    @action(detail=True, methods=["get"])
    def pjsip(self, request, pk=None):
        return Response({"config": services.pjsip_trunk_config(self.get_object())})


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def directory(request):
    """Public directory: federation-enabled, public, non-archived events of this instance."""
    if not enabled("federation"):
        return Response([])
    qs = Event.objects.filter(is_public=True).exclude(state__in=(Event.State.DRAFT, Event.State.ARCHIVED))
    return Response([services.publish_directory_entry(ev) for ev in qs.order_by("start_date")
                     if enabled("federation", ev)])


def register(router):
    router.register("federation/peers", PeerViewSet, basename="federation-peer")


urlpatterns = [path("federation/directory/", directory, name="federation-directory")]
