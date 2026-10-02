"""Emergency: routing of emergency numbers, incident log, priority handling, all-hands broadcast."""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


class EmergencyTarget(TimeStampedModel):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="emergency_targets")
    number = models.CharField(max_length=8, help_text=_("Must be one of the plan's emergency numbers, e.g. 112."))
    label = models.CharField(max_length=80, blank=True)
    destination_extension = models.ForeignKey("extensions.Extension", null=True, blank=True,
                                              on_delete=models.SET_NULL, related_name="emergency_targets",
                                              help_text=_("Security / medics group or extension."))
    fallback_number = models.CharField(max_length=32, blank=True,
                                       help_text=_("Dialed when the destination is missing/inactive (e.g. via PSTN)."))
    announce_location = models.BooleanField(default=True, help_text=_("Read the caller's location hint / RFP."))
    priority = models.PositiveSmallIntegerField(default=100)

    class Meta:
        ordering = ["-priority", "number"]
        unique_together = [("event", "number")]

    def __str__(self):
        return f"{self.number} -> {self.destination_extension_id or self.fallback_number} ({self.event.slug})"


class EmergencyIncident(TimeStampedModel):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="emergency_incidents")
    number = models.CharField(max_length=8)
    caller_number = models.CharField(max_length=32, blank=True)
    caller_extension = models.ForeignKey("extensions.Extension", null=True, blank=True, on_delete=models.SET_NULL,
                                         related_name="emergency_incidents")
    at = models.DateTimeField(auto_now_add=True)
    handled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="handled_incidents")
    notes = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-at"]

    def __str__(self):
        return f"{self.number} from {self.caller_number or '?'} at {self.at:%H:%M}"

    @property
    def is_open(self):
        return self.resolved_at is None


class BroadcastAnnouncement(TimeStampedModel):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="emergency_broadcasts")
    text = models.TextField(blank=True, help_text=_("TTS text or an audio file path known to the PBX."))
    audio = models.FileField(upload_to="emergency/", null=True, blank=True)
    group = models.ForeignKey("events.UserGroup", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="emergency_broadcasts")
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="emergency_broadcasts")
    sent_at = models.DateTimeField(auto_now_add=True)
    targets = models.PositiveIntegerField(default=0)
    # {"channels": [...], "numbers": [...], "text_broadcast": <messaging broadcast id|None>, "error": ""}
    results = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-sent_at"]

    def __str__(self):
        return f"emergency broadcast {self.sent_at:%d.%m. %H:%M} ({self.targets} targets)"
