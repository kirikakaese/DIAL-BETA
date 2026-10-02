"""PSTN breakout: trunks, outbound rules, caller-ID mapping, permissions and usage accounting."""
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


class Trunk(TimeStampedModel):
    class Transport(models.TextChoices):
        UDP = "udp", "UDP"
        TCP = "tcp", "TCP"
        TLS = "tls", "TLS"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="breakout_trunks")
    name = models.CharField(max_length=80)
    sip_host = models.CharField(max_length=200)
    port = models.PositiveIntegerField(default=5060)
    transport = models.CharField(max_length=3, choices=Transport.choices, default=Transport.UDP)
    auth_user = models.CharField(max_length=80, blank=True)
    auth_password = models.CharField(max_length=128, blank=True)
    from_domain = models.CharField(max_length=200, blank=True)
    outbound_prefix = models.CharField(max_length=8, default="0", help_text=_("Digits users dial first, e.g. 0."))
    caller_id_default = models.CharField(max_length=32, blank=True, help_text=_("E.164 number presented by default."))
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["event", "name"]

    def __str__(self):
        return f"{self.name} ({self.sip_host})"

    @property
    def endpoint_name(self):
        return f"trunk-{self.pk}"


class OutboundRule(TimeStampedModel):
    trunk = models.ForeignKey(Trunk, on_delete=models.CASCADE, related_name="rules")
    name = models.CharField(max_length=80)
    pattern = models.CharField(max_length=120, help_text=_("Regex matched against the number after the prefix."))
    allow = models.BooleanField(default=True)
    per_call_max_minutes = models.PositiveSmallIntegerField(default=0, help_text=_("0 = unlimited."))
    priority = models.PositiveSmallIntegerField(default=100, help_text=_("Lower wins."))

    class Meta:
        ordering = ["priority", "pk"]

    def __str__(self):
        return f"{'allow' if self.allow else 'deny'} {self.pattern}"


class CallerIdMapping(TimeStampedModel):
    trunk = models.ForeignKey(Trunk, on_delete=models.CASCADE, related_name="caller_ids")
    extension = models.ForeignKey("extensions.Extension", on_delete=models.CASCADE, related_name="breakout_caller_ids")
    caller_id = models.CharField(max_length=32)

    class Meta:
        unique_together = [("trunk", "extension")]

    def __str__(self):
        return f"{self.extension.number} -> {self.caller_id}"


class BreakoutPermission(TimeStampedModel):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="breakout_permissions")
    extension = models.ForeignKey("extensions.Extension", null=True, blank=True, on_delete=models.CASCADE,
                                  related_name="breakout_permissions")
    user_group = models.ForeignKey("events.UserGroup", null=True, blank=True, on_delete=models.CASCADE,
                                   related_name="breakout_permissions")
    allowed = models.BooleanField(default=True)
    daily_minutes_limit = models.PositiveIntegerField(default=60, help_text=_("0 = unlimited."))

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(extension__isnull=False) | models.Q(user_group__isnull=False),
                name="breakout_permission_has_subject",
            )
        ]

    def __str__(self):
        subj = self.extension.number if self.extension_id else f"group {self.user_group.slug}"
        return f"{subj}: {'allow' if self.allowed else 'deny'} {self.daily_minutes_limit}min/day"


class BreakoutUsage(models.Model):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="breakout_usage")
    extension = models.ForeignKey("extensions.Extension", on_delete=models.CASCADE, related_name="breakout_usage")
    date = models.DateField()
    minutes = models.FloatField(default=0)
    calls = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("extension", "date")]
        ordering = ["-date"]

    def __str__(self):
        return f"{self.extension.number} {self.date}: {self.calls} calls / {self.minutes:.1f} min"
