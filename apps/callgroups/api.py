"""Call groups REST API (``/api/v1/callgroups/``).

- ``callgroups/``                 list (``?event=<slug>``), create ``{event, number, name, strategy, ...}``,
                                  retrieve/update/delete
- ``callgroups/{id}/members/``    GET member list
- ``callgroups/{id}/add-member/`` POST ``{number, priority?}``; ``remove-member/`` POST ``{number}``
- ``callgroups/{id}/login/`` / ``logout/``  POST ``{number?}`` (defaults to the caller's own membership)
- ``callgroups/{id}/targets/``    GET ordered numbers the PBX would ring right now (+ ``waves`` by ring delay)
- ``callgroups/{id}/invite/``     POST ``{number, reason?}`` (managers); ``callgroups/{id}/invites/`` GET open ones
- ``callgroups/invites/``         GET open invitations addressed to my extensions (``?event=<slug>``)
- ``callgroups/invites/{id}/respond/`` POST ``{accept: true|false}`` (extension owner / orga)
- ``callgroups/{id}/admins/``     GET list; POST ``{user}`` (e-mail or nickname) adds; DELETE ``{user}`` removes
"""
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.api.permissions import HasScope
from apps.events.models import Event, UserGroup
from apps.extensions.models import Extension

from . import services
from .models import CallGroup, CallGroupInvite, GroupMember
from .services import CallGroupError

SCOPES = {"get": ["callgroups:read"], "default": ["callgroups:write"]}


def _check_account_event(request, event):
    acct = getattr(request, "service_account", None)
    if acct is not None and acct.event_id and acct.event_id != event.pk:
        raise PermissionDenied("token not valid for this event")


class GroupMemberSerializer(serializers.ModelSerializer):
    number = serializers.CharField(source="extension.number", read_only=True)
    owner = serializers.CharField(source="extension.owner.username", read_only=True, default=None)
    is_group = serializers.BooleanField(read_only=True)

    class Meta:
        model = GroupMember
        fields = ["id", "number", "owner", "is_group", "logged_in", "priority", "delay_s", "last_call_at",
                  "created_at"]


class InviteSerializer(serializers.ModelSerializer):
    group = serializers.CharField(source="group.number", read_only=True)
    group_id = serializers.IntegerField(read_only=True)
    number = serializers.CharField(source="extension.number", read_only=True)
    invited_by = serializers.CharField(source="invited_by.username", read_only=True, default=None)
    status = serializers.CharField(read_only=True)

    class Meta:
        model = CallGroupInvite
        fields = ["id", "group", "group_id", "number", "invited_by", "reason", "status", "accepted", "created_at",
                  "responded_at"]


class CallGroupSerializer(serializers.ModelSerializer):
    event = serializers.SlugRelatedField(slug_field="slug", queryset=Event.objects.all())
    number = serializers.CharField(max_length=16)
    name = serializers.CharField(max_length=60)
    owner = serializers.CharField(source="extension.owner.username", read_only=True, default=None)
    state = serializers.CharField(source="extension.state", read_only=True)
    user_group = serializers.SlugRelatedField(slug_field="slug", queryset=UserGroup.objects.all(), required=False,
                                              allow_null=True)
    # explicit default: a missing checkbox in form-encoded input must not mean "False"
    allow_self_service = serializers.BooleanField(required=False, default=True)
    admins = serializers.SerializerMethodField()
    members_total = serializers.SerializerMethodField()
    members_logged_in = serializers.SerializerMethodField()

    class Meta:
        model = CallGroup
        fields = ["id", "event", "number", "name", "owner", "admins", "state", "strategy", "shortcode", "ring_timeout",
                  "wrap_up_seconds", "allow_self_service", "description", "user_group", "members_total",
                  "members_logged_in", "created_at", "updated_at"]
        read_only_fields = ["id", "owner", "admins", "state", "created_at", "updated_at"]

    def get_admins(self, obj):
        return list(obj.admins.order_by("username").values_list("username", flat=True))

    def get_members_total(self, obj):
        return obj.members.count()

    def get_members_logged_in(self, obj):
        return obj.members.filter(logged_in=True).count()

    def to_representation(self, obj):
        d = super().to_representation(obj)
        d["number"] = obj.extension.number
        d["name"] = obj.name
        return d


class CallGroupViewSet(viewsets.ModelViewSet):
    serializer_class = CallGroupSerializer
    permission_classes = [IsAuthenticated, HasScope]
    required_scopes = SCOPES

    def get_queryset(self):
        qs = CallGroup.objects.select_related("extension__owner", "event", "user_group").exclude(
            extension__state__in=[Extension.State.DELETED, Extension.State.REJECTED])
        slug = self.request.query_params.get("event")
        if slug:
            event = get_object_or_404(Event, slug=slug)
            _check_account_event(self.request, event)
            qs = qs.filter(event=event)
        user = self.request.user
        if not user.is_superuser:
            qs = qs.filter(event__in=Event.objects.visible_to(user))
        return qs

    def _manage_or_403(self, group):
        if not services.can_manage(self.request.user, group):
            raise PermissionDenied("group owner or orga required")

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        _check_account_event(request, d["event"])
        if d.get("user_group") and d["user_group"].event_id != d["event"].pk:
            raise ValidationError({"user_group": "not a group of this event"})
        try:
            group = services.create_group(
                d["event"], request.user, d["number"], d["name"], d.get("strategy", CallGroup.Strategy.RING_ALL),
                request=request, description=d.get("description", ""), ring_timeout=d.get("ring_timeout", 20),
                wrap_up_seconds=d.get("wrap_up_seconds", 0), allow_self_service=d.get("allow_self_service", True),
                user_group=d.get("user_group"), shortcode=d.get("shortcode", ""),
            )
        except CallGroupError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(self.get_serializer(group).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        group = self.get_object()
        self._manage_or_403(group)
        ser = self.get_serializer(group, data=request.data, partial=kwargs.pop("partial", False))
        ser.is_valid(raise_exception=True)
        d = dict(ser.validated_data)
        d.pop("event", None)
        d.pop("number", None)
        try:
            services.update_group(group, request.user, request=request, **d)
        except CallGroupError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(self.get_serializer(group).data)

    def perform_destroy(self, instance):
        self._manage_or_403(instance)
        services.delete_group(instance, self.request.user, request=self.request)

    @action(detail=True, methods=["get"])
    def members(self, request, pk=None):
        group = self.get_object()
        qs = group.members.select_related("extension__owner").order_by("priority", "extension__number")
        return Response(GroupMemberSerializer(qs, many=True).data)

    @action(detail=True, methods=["post"], url_path="add-member")
    def add_member(self, request, pk=None):
        group = self.get_object()
        self._manage_or_403(group)
        ext = services._active_extension(group.event, str(request.data.get("number") or ""))
        if ext is None:
            raise ValidationError({"number": "unknown or inactive extension"})
        try:
            member = services.add_member(group, ext, request.user, via="api", request=request,
                                         priority=int(request.data.get("priority") or 0),
                                         delay_s=int(request.data.get("delay_s") or 0))
        except CallGroupError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(GroupMemberSerializer(member).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="remove-member")
    def remove_member(self, request, pk=None):
        group = self.get_object()
        member = get_object_or_404(group.members, extension__number=str(request.data.get("number") or ""))
        if not (services.can_manage(request.user, group) or member.extension.owner_id == request.user.pk):
            raise PermissionDenied
        services.remove_member(member, request.user, via="api", request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _toggle(self, request, state):
        group = self.get_object()
        number = str(request.data.get("number") or "").strip()
        qs = group.members.select_related("extension", "group__extension")
        if number:
            member = get_object_or_404(qs, extension__number=number)
            members = [member]
        else:
            members = list(qs.filter(extension__owner=request.user))
            if not members:
                raise ValidationError({"number": "you have no extension in this group; pass number"})
        for m in members:
            if not services.can_toggle(request.user, m):
                raise PermissionDenied
            (services.login if state else services.logout)(m, "api", actor=request.user, request=request)
        return Response(GroupMemberSerializer(members, many=True).data)

    @action(detail=True, methods=["post"])
    def login(self, request, pk=None):
        return self._toggle(request, True)

    @action(detail=True, methods=["post"])
    def logout(self, request, pk=None):
        return self._toggle(request, False)

    @action(detail=True, methods=["get"])
    def targets(self, request, pk=None):
        group = self.get_object()
        return Response({"group": group.number, "strategy": group.strategy,
                         "serial": group.is_serial, "targets": services.dial_targets(group.extension),
                         "waves": services.dial_waves(group.extension),
                         "callerid_prefix": group.callerid_prefix})

    # ----------------------------------------------------------------- admins

    @action(detail=True, methods=["get", "post", "delete"])
    def admins(self, request, pk=None):
        group = self.get_object()
        if request.method != "GET":
            if not services.is_owner_or_orga(request.user, group):
                raise PermissionDenied("group owner or orga required")
            target = services.find_user(str(request.data.get("user") or ""))
            if target is None:
                raise ValidationError({"user": "unknown user (e-mail or nickname)"})
            try:
                if request.method == "POST":
                    services.add_admin(group, target, request.user, request=request)
                else:
                    services.remove_admin(group, target, request.user, request=request)
            except CallGroupError as exc:
                raise ValidationError({"detail": str(exc)})
        return Response(list(group.admins.order_by("username").values_list("username", flat=True)))

    # ----------------------------------------------------------------- invites

    @action(detail=True, methods=["post"])
    def invite(self, request, pk=None):
        group = self.get_object()
        self._manage_or_403(group)
        ext = services._active_extension(group.event, str(request.data.get("number") or ""))
        if ext is None:
            raise ValidationError({"number": "unknown or inactive extension"})
        try:
            inv = services.invite(group, ext, request.user, str(request.data.get("reason") or ""), request=request)
        except CallGroupError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(InviteSerializer(inv).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="invites")
    def group_invites(self, request, pk=None):
        group = self.get_object()
        self._manage_or_403(group)
        qs = group.open_invites().select_related("group__extension", "extension", "invited_by")
        return Response(InviteSerializer(qs, many=True).data)

    @action(detail=False, methods=["get"], url_path="invites")
    def my_invites(self, request):
        """Open invitations for my extensions; ``?event=<slug>`` narrows to one event."""
        qs = CallGroupInvite.objects.filter(extension__owner=request.user, responded_at__isnull=True,
                                            group__in=self.get_queryset())
        return Response(InviteSerializer(qs.select_related("group__extension", "extension", "invited_by"),
                                         many=True).data)

    @action(detail=True, methods=["post"], url_path="invites/(?P<ipk>[0-9]+)/cancel")
    def cancel_invite(self, request, pk=None, ipk=None):
        group = self.get_object()
        inv = get_object_or_404(group.invites, pk=ipk)
        try:
            services.cancel_invite(inv, request.user, request=request)
        except CallGroupError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(InviteSerializer(inv).data)

    @action(detail=False, methods=["post"], url_path="invites/(?P<ipk>[0-9]+)/respond")
    def respond(self, request, ipk=None):
        inv = get_object_or_404(CallGroupInvite.objects.select_related("group__extension", "extension"),
                                pk=ipk, group__in=self.get_queryset())
        raw = request.data.get("accept")
        accept = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes", "on")
        try:
            services.respond(inv, request.user, accept, request=request)
        except CallGroupError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(InviteSerializer(inv).data)


def register(router):
    router.register("callgroups", CallGroupViewSet, basename="callgroup")
