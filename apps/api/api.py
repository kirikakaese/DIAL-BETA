"""Core API: availability check, events, extensions, devices, number plans, audit."""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.urls import path
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.core.models import AuditLog
from apps.devices.models import Device, DeviceBinding
from apps.events.models import Event, EventMembership, UserGroup, validate_schedule
from apps.extensions import services
from apps.extensions.models import Extension, ExtensionType
from apps.numbering.models import NumberPlan, NumberRange

from .permissions import HasScope, IsEventOrga, IsOwnerOrOrga

# --- serializers --------------------------------------------------------------

class EventSerializer(serializers.ModelSerializer):
    next_scheduled_transition = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = ["id", "name", "slug", "description", "state", "is_public", "start_date", "end_date",
                  "location", "timezone", "primary_color", "accent_color", "announcement", "sip_domain",
                  "dial_prefix", "max_extensions_per_user", "allow_guest_extensions", "allow_breakout",
                  "registration_opens_at", "goes_live_at", "archives_at", "next_scheduled_transition",
                  "created_at"]
        read_only_fields = ["id", "created_at"]

    def get_next_scheduled_transition(self, obj):
        nxt = obj.next_scheduled_transition()
        if nxt is None:
            return None
        return {"state": nxt[0], "at": nxt[1].isoformat()}

    def validate(self, attrs):
        def val(name):
            return attrs[name] if name in attrs else getattr(self.instance, name, None)

        try:
            validate_schedule(val("state") or Event.State.DRAFT, val("registration_opens_at"), val("goes_live_at"),
                              val("archives_at"))
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict) from exc
        return attrs


class UserGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserGroup
        fields = ["id", "name", "slug", "description"]


class MembershipSerializer(serializers.ModelSerializer):
    user = serializers.SlugRelatedField(slug_field="username", read_only=True)
    groups = serializers.SlugRelatedField(slug_field="slug", many=True, read_only=True)

    class Meta:
        model = EventMembership
        fields = ["id", "user", "role", "groups", "created_at"]


class NumberRangeSerializer(serializers.ModelSerializer):
    allowed_groups = serializers.SlugRelatedField(slug_field="slug", many=True, read_only=True)

    class Meta:
        model = NumberRange
        fields = ["id", "name", "description", "priority", "is_active", "prefix", "pattern", "min_length",
                  "max_length", "mode", "allowed_roles", "allowed_groups", "allowed_types", "is_vanity",
                  "quota_per_user"]


class NumberPlanSerializer(serializers.ModelSerializer):
    ranges = NumberRangeSerializer(many=True, read_only=True)

    class Meta:
        model = NumberPlan
        fields = ["min_length", "max_length", "default_requires_approval", "default_allowed",
                  "test_ringback_number", "wakeup_service_number", "site_survey_number", "echo_test_number",
                  "voicemail_number", "dect_claim_number", "announcement_record_number", "emergency_numbers",
                  "callback_request_code", "callback_cancel_code", "group_login_code", "group_logout_code",
                  "forward_set_code", "forward_clear_code", "forward_busy_code", "forward_noanswer_code", "ranges"]


class DeviceSerializer(serializers.ModelSerializer):
    owner = serializers.SlugRelatedField(slug_field="username", read_only=True)
    event = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    extensions = serializers.SerializerMethodField()

    class Meta:
        model = Device
        fields = ["id", "event", "owner", "type", "name", "state", "ipei", "handset_model", "omm_ppn",
                  "last_seen_rfp", "last_seen_at", "battery_percent", "rssi", "sip_username", "sip_transport",
                  "sip_registered_at", "mac_address", "extensions", "created_at"]
        read_only_fields = ["id", "state", "omm_ppn", "last_seen_rfp", "last_seen_at", "battery_percent",
                            "rssi", "sip_username", "sip_registered_at", "created_at"]

    def get_extensions(self, obj):
        return [b.extension.number for b in obj.bindings.select_related("extension")]


class DeviceSecretSerializer(DeviceSerializer):
    """Includes SIP password / subscription PIN / softphone QR payloads - only for owner/orga."""

    class Meta(DeviceSerializer.Meta):
        fields = DeviceSerializer.Meta.fields + ["sip_password", "subscription_pin",
                                                  "subscription_pin_expires_at", "sip_uri", "softphone_links"]

    sip_uri = serializers.CharField(read_only=True)
    softphone_links = serializers.SerializerMethodField()

    def get_softphone_links(self, obj):
        return obj.softphone_links() if obj.type == "sip" else None


class ExtensionSerializer(serializers.ModelSerializer):
    owner = serializers.SlugRelatedField(slug_field="username", read_only=True)
    event = serializers.SlugRelatedField(slug_field="slug", queryset=Event.objects.all())
    devices = DeviceSerializer(source="bound_devices", many=True, read_only=True)
    # Trunks: number of trailing wildcard digits of the block (1-3, i.e. 10/100/1000 numbers); writable on create
    block_digits = serializers.IntegerField(required=False, min_value=1, max_value=3, allow_null=True)
    block_range = serializers.SerializerMethodField()

    class Meta:
        model = Extension
        fields = ["id", "event", "number", "type", "owner", "state", "display_name", "description",
                  "location_hint", "in_phonebook", "ring_strategy", "ring_timeout", "forward_busy",
                  "forward_noanswer", "forward_unconditional", "allow_callback", "priority", "is_temporary",
                  "expires_at", "config", "block_digits", "block_range", "request_note", "moderation_note",
                  "provisioned_at", "provision_error", "dect_claim_code", "devices", "created_at", "updated_at"]
        read_only_fields = ["id", "owner", "state", "moderation_note", "provisioned_at", "provision_error",
                            "is_temporary", "expires_at", "priority", "dect_claim_code", "created_at", "updated_at"]

    def get_block_range(self, obj):
        return list(obj.block_range()) if obj.is_trunk and obj.block_digits else None

    def to_representation(self, instance):
        instance.bound_devices = [b.device for b in instance.bindings.select_related("device")]
        data = super().to_representation(instance)
        data["block_digits"] = instance.block_digits or None
        return data


class AuditSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = ["id", "created_at", "actor_repr", "action", "target_repr", "message", "changes"]


# --- views --------------------------------------------------------------------

class EventViewSet(viewsets.ModelViewSet):
    serializer_class = EventSerializer
    lookup_field = "slug"
    permission_classes = [IsAuthenticated, HasScope, IsEventOrga]
    required_scopes = {"get": ["events:read"], "default": ["events:write"]}
    search_fields = ["name", "slug", "location"]
    filterset_fields = ["state", "is_public"]

    def get_queryset(self):
        return Event.objects.visible_to(self.request.user)

    def get_permissions(self):
        if self.action in ("list", "retrieve", "number_plan", "members", "groups"):
            return [IsAuthenticated(), HasScope()]
        return super().get_permissions()

    def perform_create(self, serializer):
        if not self.request.user.is_superuser:
            self.permission_denied(self.request, message="Only global admins create events.")
        ev = serializer.save()
        EventMembership.objects.create(event=ev, user=self.request.user, role="admin")
        NumberPlan.objects.get_or_create(event=ev)

    def perform_destroy(self, instance):
        if not self.request.user.is_superuser:
            self.permission_denied(self.request, message="Only global admins delete events.")
        super().perform_destroy(instance)

    @action(detail=True, methods=["get"], url_path="number-plan")
    def number_plan(self, request, slug=None):
        ev = self.get_object()
        return Response(NumberPlanSerializer(services.get_plan(ev)).data)

    @action(detail=True, methods=["get"])
    def members(self, request, slug=None):
        ev = self.get_object()
        if not request.user.is_helpdesk(ev):
            return Response(status=403)
        return Response(MembershipSerializer(ev.memberships.select_related("user"), many=True).data)

    @action(detail=True, methods=["get"])
    def groups(self, request, slug=None):
        return Response(UserGroupSerializer(self.get_object().groups.all(), many=True).data)

    @action(detail=True, methods=["post"])
    def transition(self, request, slug=None):
        ev = self.get_object()
        try:
            ev.transition(request.data.get("state"), actor=request.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(EventSerializer(ev).data)

    @action(detail=True, methods=["post"])
    def clone(self, request, slug=None):
        ev = self.get_object()
        if not request.user.is_superuser:
            self.permission_denied(request, message="Only global admins create events.")
        d = request.data
        new = ev.clone(name=d["name"], slug=d["slug"], start_date=d["start_date"], end_date=d["end_date"],
                       actor=request.user)
        return Response(EventSerializer(new).data, status=201)

    @action(detail=True, methods=["get"])
    def audit(self, request, slug=None):
        ev = self.get_object()
        qs = AuditLog.objects.filter(event=ev)[:500]
        return Response(AuditSerializer(qs, many=True).data)

    @action(detail=True, methods=["get"], url_path="export")
    def export(self, request, slug=None):
        from apps.events.export import export_event

        return Response(export_event(self.get_object()))


class ExtensionViewSet(viewsets.ModelViewSet):
    serializer_class = ExtensionSerializer
    permission_classes = [IsAuthenticated, HasScope, IsOwnerOrOrga]
    required_scopes = {"get": ["extensions:read"], "default": ["extensions:write"]}
    filterset_fields = ["event__slug", "type", "state", "in_phonebook"]
    search_fields = ["number", "display_name", "owner__username", "description"]
    ordering_fields = ["number", "created_at"]

    def get_queryset(self):
        u = self.request.user
        qs = Extension.objects.select_related("event", "owner").exclude(state=Extension.State.DELETED)
        acct = getattr(self.request, "service_account", None)
        if acct is not None and acct.event_id:
            qs = qs.filter(event_id=acct.event_id)
        if u.is_superuser:
            return qs
        orga_events = EventMembership.objects.filter(user=u, role__in=("orga", "admin", "helpdesk")).values("event")
        return qs.filter(Q(owner=u) | Q(event__in=orga_events) | Q(in_phonebook=True, state="active"))

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = dict(ser.validated_data)
        event = d.pop("event")
        number = d.pop("number")
        ext_type = d.pop("type", ExtensionType.DECT)
        block_digits = d.pop("block_digits", None)
        if block_digits:
            d["config"] = {**(d.get("config") or {}), "block_digits": block_digits}
        try:
            ext = services.register(event, request.user, number, ext_type, request=request, **d)
        except services.ExtensionError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(self.get_serializer(ext).data, status=201)

    def perform_update(self, serializer):
        d = dict(serializer.validated_data)
        d.pop("event", None)
        d.pop("number", None)
        d.pop("type", None)
        d.pop("block_digits", None)  # the block of a trunk is fixed after registration
        services.update(serializer.instance, self.request.user, request=self.request, **d)

    def perform_destroy(self, instance):
        services.delete(instance, self.request.user, request=self.request)

    def _moderate(self, request, fn):
        ext = self.get_object()
        if not request.user.is_orga(ext.event):
            return Response(status=403)
        try:
            fn(ext, request.user, request.data.get("note", ""), request=request)
        except services.ExtensionError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(self.get_serializer(ext).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._moderate(request, services.approve)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        return self._moderate(request, services.reject)

    @action(detail=True, methods=["post"])
    def suspend(self, request, pk=None):
        return self._moderate(request, services.suspend)

    @action(detail=True, methods=["post"])
    def reactivate(self, request, pk=None):
        ext = self.get_object()
        if not request.user.is_orga(ext.event):
            return Response(status=403)
        services.reactivate(ext, request.user, request=request)
        return Response(self.get_serializer(ext).data)

    @action(detail=True, methods=["post"])
    def provision(self, request, pk=None):
        from apps.extensions.tasks import provision_extension

        ext = self.get_object()
        provision_extension.delay(str(ext.pk))
        return Response({"queued": True})

    @action(detail=True, methods=["post"])
    def transfer(self, request, pk=None):
        from apps.accounts.models import User

        ext = self.get_object()
        to = User.objects.filter(Q(username=request.data.get("to")) | Q(email=request.data.get("to"))).first()
        if to is None:
            return Response({"detail": "Recipient not found"}, status=404)
        try:
            tr = services.start_transfer(ext, request.user, to, request=request)
        except services.ExtensionError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response({"token": tr.token, "expires_at": tr.expires_at})

    @action(detail=True, methods=["post"], url_path="bind-device")
    def bind_device(self, request, pk=None):
        ext = self.get_object()
        dev = get_object_or_404(Device, pk=request.data.get("device"), event=ext.event)
        if dev.owner_id != request.user.pk and not request.user.is_orga(ext.event):
            return Response(status=403)
        try:
            services.validate_device_binding(ext, dev.type, device=dev)
        except services.ExtensionError as exc:
            return Response({"detail": str(exc)}, status=400)
        DeviceBinding.objects.get_or_create(extension=ext, device=dev,
                                            defaults={"priority": request.data.get("priority", 0)})
        from apps.extensions.tasks import provision_extension

        provision_extension.delay(str(ext.pk))
        return Response(self.get_serializer(ext).data)

    @action(detail=False, methods=["get"])
    def portable(self, request):
        slug = request.query_params.get("event")
        ev = get_object_or_404(Event, slug=slug)
        return Response(self.get_serializer(services.portable_extensions(request.user, ev), many=True).data)

    @action(detail=True, methods=["post"])
    def port(self, request, pk=None):
        ext = self.get_object()
        ev = get_object_or_404(Event, slug=request.data.get("event"))
        try:
            new = services.port(ext, ev, request.user, request=request)
        except services.ExtensionError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(self.get_serializer(new).data, status=201)

    def _staff_event(self, request, *, orga=True):
        """Event from ``?event=`` / body for orga (or helpdesk) tools; ``(event, None)`` or ``(None, response)``."""
        slug = request.query_params.get("event") or request.data.get("event")
        ev = Event.objects.filter(slug=slug).first() if slug else None
        if ev is None:
            return None, Response({"detail": "unknown event"}, status=404)
        acct = getattr(request, "service_account", None)
        if acct is not None and acct.event_id and acct.event_id != ev.pk:
            return None, Response({"detail": "token not valid for this event"}, status=403)
        allowed = request.user.is_orga(ev) if orga else request.user.is_helpdesk(ev)
        if not allowed:
            return None, Response({"detail": "orga role required" if orga else "helpdesk role required"},
                                  status=403)
        return ev, None

    @extend_schema(
        parameters=[OpenApiParameter("event", str, required=True)],
        request={"application/json": {"type": "object", "properties": {
            "csv": {"type": "string"}, "dry_run": {"type": "boolean"}, "create_users": {"type": "boolean"},
            "allow_ownerless": {"type": "boolean"}}, "required": ["csv"]}},
        responses={200: dict},
    )
    @action(detail=False, methods=["post"], url_path="import")
    def import_csv(self, request):
        """Bulk import extensions (and missing users) from CSV text - orga only. ``dry_run`` returns the plan."""
        from apps.extensions import csv_import

        ev, err = self._staff_event(request)
        if err:
            return err
        text = request.data.get("csv")
        if not isinstance(text, str) or not text.strip():
            return Response({"detail": "csv text is required"}, status=400)
        dry_run = _truthy(request.data.get("dry_run", False))
        create_users = _truthy(request.data.get("create_users", False))
        allow_ownerless = _truthy(request.data.get("allow_ownerless", True))
        try:
            rows = csv_import.parse_csv(text)
        except csv_import.CSVImportError as exc:
            return Response({"detail": str(exc)}, status=400)
        if dry_run:
            plan = csv_import.preview(ev, rows, create_users=create_users, allow_ownerless=allow_ownerless,
                                      actor=request.user)
            errors = [{"line": r.row.line, "number": r.row.number, "action": r.action,
                       "message": "; ".join(str(m) for m in r.messages)}
                      for r in plan.rows if r.action != csv_import.ACTION_CREATE]
            return Response({"plan": plan.as_list(), "applied": 0, "dry_run": True, "errors": errors,
                             "unknown_columns": plan.unknown_columns})
        result = csv_import.apply(ev, rows, request.user, create_users=create_users,
                                  allow_ownerless=allow_ownerless, request=request)
        return Response({**result.as_dict(), "dry_run": False, "unknown_columns": result.plan.unknown_columns})

    @extend_schema(
        parameters=[OpenApiParameter("event", str, required=True), OpenApiParameter("number", str, required=True)],
        responses={200: dict},
    )
    @action(detail=False, methods=["get"])
    def history(self, request):
        """Timeline of one number: every extension that carried it (this event + events the caller staffs)
        and the audit entries of those extensions. Orga/helpdesk only."""
        from apps.extensions.history import looks_like_number, number_history

        ev, err = self._staff_event(request, orga=False)
        if err:
            return err
        number = (request.query_params.get("number") or "").strip()
        if not looks_like_number(number):
            return Response({"detail": "number (digits) is required"}, status=400)
        return Response(number_history(ev, number, request.user))


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class DeviceViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasScope, IsOwnerOrOrga]
    required_scopes = {"get": ["devices:read"], "default": ["devices:write"]}
    filterset_fields = ["event__slug", "type", "state"]
    search_fields = ["ipei", "sip_username", "name", "owner__username", "mac_address"]

    def get_queryset(self):
        u = self.request.user
        qs = Device.objects.select_related("event", "owner", "last_seen_rfp")
        if u.is_superuser:
            return qs
        orga_events = EventMembership.objects.filter(user=u, role__in=("orga", "admin", "helpdesk")).values("event")
        return qs.filter(Q(owner=u) | Q(event__in=orga_events))

    def get_serializer_class(self):
        if self.action in ("retrieve", "create", "update", "partial_update", "new_pin", "rotate"):
            return DeviceSecretSerializer
        return DeviceSerializer

    def perform_create(self, serializer):
        event = get_object_or_404(Event, slug=self.request.data.get("event"))
        ext_id = self.request.data.get("extension")
        ext = get_object_or_404(Extension, pk=ext_id, event=event) if ext_id else None
        if ext is not None and (ext.owner_id == self.request.user.pk or self.request.user.is_orga(event)):
            try:
                services.validate_device_binding(ext, serializer.validated_data.get("type"))
            except services.ExtensionError as exc:
                raise serializers.ValidationError({"extension": str(exc)}) from exc
        else:
            ext = None
        dev = serializer.save(event=event, owner=self.request.user)
        dev.ensure_sip_credentials()
        if dev.type == "dect":
            dev.issue_subscription_pin()
        if ext is not None:
            DeviceBinding.objects.create(extension=ext, device=dev)
            from apps.extensions.tasks import provision_extension

            provision_extension.delay(str(ext.pk))

    @action(detail=True, methods=["post"], url_path="new-pin")
    def new_pin(self, request, pk=None):
        dev = self.get_object()
        dev.issue_subscription_pin()
        from apps.dect.provisioning import provision_device

        provision_device(dev)
        return Response(self.get_serializer(dev).data)

    @action(detail=True, methods=["post"])
    def rotate(self, request, pk=None):
        dev = self.get_object()
        dev.rotate_sip_password()
        from apps.pbx import outbox

        outbox.enqueue("sync_device", event=dev.event, target=dev)
        return Response(self.get_serializer(dev).data)

    @action(detail=True, methods=["get"])
    def qr(self, request, pk=None):
        from django.http import Http404, HttpResponse

        from apps.devices.qr import qr_png

        dev = self.get_object()
        links = dev.softphone_links()
        client = request.query_params.get("client", "generic")
        if client not in links:
            raise Http404
        return HttpResponse(qr_png(links[client]), content_type="image/png")


@extend_schema(
    parameters=[OpenApiParameter("event", str, required=True), OpenApiParameter("number", str, required=True),
                OpenApiParameter("type", str),
                OpenApiParameter("block_digits", int, description="Trunks: check the whole block (1-3 digits)")],
    responses={200: dict},
)
@api_view(["GET"])
@permission_classes([AllowAny])
def availability(request):
    """Live availability check used by the self-service portal while typing."""
    ev = get_object_or_404(Event, slug=request.query_params.get("event"))
    number = request.query_params.get("number", "")
    ext_type = request.query_params.get("type") or None
    user = request.user if request.user.is_authenticated else None
    try:
        block_digits = int(request.query_params.get("block_digits") or 0)
    except ValueError:
        block_digits = 0
    if ext_type != ExtensionType.TRUNK:
        block_digits = 0
    return Response(services.check_availability(ev, number, user=user, extension_type=ext_type,
                                                block_digits=block_digits).as_dict())


@extend_schema(
    parameters=[OpenApiParameter("event", str, required=True), OpenApiParameter("type", str)],
    responses={200: dict},
)
@api_view(["GET"])
@permission_classes([AllowAny])
def random_number(request):
    """A random free number the caller could register instantly (from the event's pools); ``number`` is
    ``null`` when none was found."""
    from apps.numbering.services import random_free_number

    ev = get_object_or_404(Event, slug=request.query_params.get("event"))
    ext_type = request.query_params.get("type") or None
    user = request.user if request.user.is_authenticated else None
    return Response({"number": random_free_number(ev, user=user, extension_type=ext_type)})


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    """Server-wide default backends plus - with ``?event=<slug>`` - the venue infrastructure of one event.

    Without ``event`` only the defaults are checked (they may be simulators on a multi-event server);
    per-event connections are what matters at a venue, so monitoring should poll ``?event=``.
    """
    from apps.dect import get_dect
    from apps.pbx import get_pbx

    event = None
    slug = request.query_params.get("event")
    if slug:
        event = Event.objects.filter(slug=slug).first()
        if event is None:
            return Response({"ok": False, "error": "unknown event"}, status=404)
    out = {"ok": True, "event": event.slug if event else None, "pbx": {}, "dect": {}}
    try:
        out["pbx"] = get_pbx(event).health()
    except Exception as exc:  # noqa: BLE001
        out["pbx"] = {"ok": False, "error": str(exc)}
    try:
        out["dect"] = get_dect(event).health()
    except Exception as exc:  # noqa: BLE001
        out["dect"] = {"ok": False, "error": str(exc)}
    out["ok"] = bool(out["pbx"].get("ok")) and bool(out["dect"].get("ok"))
    return Response(out, status=200 if out["ok"] else 503)


@api_view(["GET"])
def me(request):
    u = request.user
    return Response({
        "id": str(u.pk), "username": u.username, "email": u.email, "display_name": u.display_name,
        "is_superuser": u.is_superuser,
        "memberships": MembershipSerializer(u.memberships.select_related("event"), many=True).data,
        "service_account": getattr(getattr(request, "service_account", None), "name", None),
    })


def register(router):
    router.register("events", EventViewSet, basename="event")
    router.register("extensions", ExtensionViewSet, basename="extension")
    router.register("devices", DeviceViewSet, basename="device")


urlpatterns = [
    path("availability/", availability, name="availability"),
    path("random-number/", random_number, name="random_number"),
    path("health/", health, name="health"),
    path("me/", me, name="me"),
]
