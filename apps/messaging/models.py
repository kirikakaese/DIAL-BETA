"""Messaging: text messages to DECT handsets (via OMM) and orga broadcasts."""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel

MAX_TEXT = 160 * 3


class Message(TimeStampedModel):
    class State(models.TextChoices):
        QUEUED = "queued", _("Queued")
        SENT = "sent", _("Sent")
        FAILED = "failed", _("Failed")
        DELIVERED = "delivered", _("Delivered")

    class Direction(models.TextChoices):
        OUT = "out", _("Outbound (PET → handset)")
        IN = "in", _("Inbound (handset → PET)")

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="messages")
    direction = models.CharField(max_length=3, choices=Direction.choices, default=Direction.OUT)
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="sent_messages")
    sender_extension = models.ForeignKey("extensions.Extension", null=True, blank=True, on_delete=models.SET_NULL,
                                         related_name="sent_messages")
    recipient_extension = models.ForeignKey("extensions.Extension", null=True, blank=True,
                                            on_delete=models.SET_NULL, related_name="received_messages")
    recipient_group = models.ForeignKey("events.UserGroup", null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name="messages")
    broadcast = models.ForeignKey("Broadcast", null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="messages")
    text = models.CharField(max_length=MAX_TEXT)
    state = models.CharField(max_length=10, choices=State.choices, default=State.QUEUED, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"msg #{self.pk} -> {self.recipient_extension_id or self.recipient_group_id} [{self.state}]"


class Broadcast(TimeStampedModel):
    class Target(models.TextChoices):
        ALL = "all", _("All active extensions")
        GROUP = "group", _("One user group")

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="broadcasts")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="broadcasts")
    target = models.CharField(max_length=5, choices=Target.choices, default=Target.ALL)
    group = models.ForeignKey("events.UserGroup", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="broadcasts")
    text = models.CharField(max_length=MAX_TEXT)
    sent_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"broadcast #{self.pk} ({self.target}) {self.sent_count}/{self.sent_count + self.failed_count}"
