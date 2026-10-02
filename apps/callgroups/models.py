"""Call groups (hunt groups): a ``group`` extension that rings the logged-in member extensions."""
import secrets

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel
from apps.extensions.models import ExtensionType


def _new_token():
    return secrets.token_urlsafe(24)


class CallGroup(TimeStampedModel):
    class Strategy(models.TextChoices):
        RING_ALL = "ringall", _("Ring all members at once")
        ROUND_ROBIN = "roundrobin", _("Round robin (rotate who rings first)")
        LONGEST_IDLE = "longestidle", _("Longest idle member first")

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="callgroups")
    extension = models.OneToOneField("extensions.Extension", on_delete=models.CASCADE, related_name="callgroup")
    strategy = models.CharField(max_length=16, choices=Strategy.choices, default=Strategy.RING_ALL)
    ring_timeout = models.PositiveSmallIntegerField(
        default=20, help_text=_("Seconds to ring (per member for serial strategies)."))
    wrap_up_seconds = models.PositiveSmallIntegerField(
        default=0, help_text=_("Members are skipped for this many seconds after their last group call."))
    allow_self_service = models.BooleanField(
        default=True, help_text=_("Members may log in/out themselves (portal and feature codes)."))
    description = models.CharField(max_length=200, blank=True)
    user_group = models.ForeignKey("events.UserGroup", null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="callgroups",
                                   help_text=_("Extensions of this user group's members join automatically."))
    admins = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="administered_callgroups",
                                    help_text=_("Users who may manage members and settings besides the owner."))
    shortcode = models.CharField(
        max_length=8, blank=True,
        help_text=_("Short label (e.g. SEC, MED) shown in front of the caller name when the group rings a member."))

    class Meta:
        ordering = ["extension__number"]
        verbose_name = _("call group")

    def __str__(self):
        return f"{self.name} ({self.number})"

    @property
    def number(self) -> str:
        return self.extension.number

    @property
    def name(self) -> str:
        return self.extension.display_name or self.extension.number

    @property
    def owner(self):
        return self.extension.owner

    @property
    def is_serial(self) -> bool:
        return self.strategy != self.Strategy.RING_ALL

    @property
    def callerid_prefix(self) -> str:
        """``"[SEC] "`` for shortcode ``SEC``, ``""`` without a shortcode."""
        code = (self.shortcode or "").strip()
        return f"[{code}] " if code else ""

    def logged_in_members(self):
        return self.members.filter(logged_in=True)

    def open_invites(self):
        return self.invites.filter(responded_at__isnull=True)


class GroupMember(TimeStampedModel):
    group = models.ForeignKey(CallGroup, on_delete=models.CASCADE, related_name="members")
    extension = models.ForeignKey("extensions.Extension", on_delete=models.CASCADE, related_name="group_memberships")
    logged_in = models.BooleanField(default=True)
    priority = models.PositiveSmallIntegerField(default=0, help_text=_("Lower rings first (ties broken by strategy)."))
    delay_s = models.PositiveSmallIntegerField(
        default=0, help_text=_("Ring-all only: seconds to wait before this member starts ringing."))
    last_call_at = models.DateTimeField(null=True, blank=True)
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="+")

    class Meta:
        unique_together = [("group", "extension")]
        ordering = ["priority", "extension__number"]
        verbose_name = _("group member")

    def __str__(self):
        return f"{self.extension.number} in {self.group}"

    @property
    def number(self) -> str:
        return self.extension.number

    @property
    def is_group(self) -> bool:
        """Nested group: this member is itself a call group extension."""
        return self.extension.type == ExtensionType.GROUP


class CallGroupInvite(models.Model):
    """An extension owner is asked to join a group; accepting creates the :class:`GroupMember`."""

    group = models.ForeignKey(CallGroup, on_delete=models.CASCADE, related_name="invites")
    extension = models.ForeignKey("extensions.Extension", on_delete=models.CASCADE, related_name="callgroup_invites")
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    accepted = models.BooleanField(null=True, blank=True, help_text=_("Empty while open or when cancelled."))
    token = models.CharField(max_length=64, unique=True, default=_new_token, editable=False)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("call group invite")
        constraints = [
            models.UniqueConstraint(fields=["group", "extension"], condition=models.Q(responded_at__isnull=True),
                                    name="callgroups_unique_open_invite"),
        ]

    def __str__(self):
        return f"{self.extension.number} invited to {self.group} ({self.status})"

    @property
    def is_open(self) -> bool:
        return self.responded_at is None

    @property
    def status(self) -> str:
        if self.is_open:
            return "open"
        if self.accepted is None:
            return "cancelled"
        return "accepted" if self.accepted else "declined"


class GroupLoginLog(models.Model):
    class Action(models.TextChoices):
        LOGIN = "login", _("Login")
        LOGOUT = "logout", _("Logout")
        ADDED = "added", _("Added")
        REMOVED = "removed", _("Removed")

    member = models.ForeignKey(GroupMember, on_delete=models.CASCADE, related_name="login_log")
    action = models.CharField(max_length=8, choices=Action.choices)
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    via = models.CharField(max_length=20, blank=True, help_text=_("web / api / feature-code / auto"))

    class Meta:
        ordering = ["-at"]
        verbose_name = _("group login log")

    def __str__(self):
        return f"{self.at:%H:%M:%S} {self.member.number} {self.action} ({self.via})"
