"""Conference rooms (ConfBridge) attached to ``conference`` extensions."""
from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


class ConferenceRoom(TimeStampedModel):
    extension = models.OneToOneField("extensions.Extension", on_delete=models.CASCADE, related_name="conference_room")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="conference_rooms")
    name = models.CharField(max_length=80, blank=True)
    pin = models.CharField(max_length=8, blank=True, validators=[RegexValidator(r"^\d{4,8}$")],
                           help_text=_("4-8 digits; leave blank for an open room."))
    max_participants = models.PositiveSmallIntegerField(default=20)
    record = models.BooleanField(default=False)
    music_on_hold = models.BooleanField(default=True, help_text=_("Play MoH while alone in the room."))
    announce_join = models.BooleanField(default=True, help_text=_("Play a tone when someone joins/leaves."))
    is_public = models.BooleanField(default=True, verbose_name=_("Listed for everyone"))

    class Meta:
        ordering = ["extension__number"]

    def __str__(self):
        return f"conference {self.extension.number}"

    @property
    def event(self):
        return self.extension.event

    @property
    def number(self):
        return self.extension.number

    @property
    def is_open(self):
        return not self.pin

    @property
    def active_participants(self):
        return self.participants.filter(left_at__isnull=True)


class ConferenceParticipant(TimeStampedModel):
    room = models.ForeignKey(ConferenceRoom, on_delete=models.CASCADE, related_name="participants")
    channel_id = models.CharField(max_length=120, db_index=True)
    caller_number = models.CharField(max_length=32, blank=True)
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["joined_at"]

    def __str__(self):
        return f"{self.caller_number} in {self.room}"
