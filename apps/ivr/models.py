"""IVR: announcements, DTMF menus and fun services attached to extensions."""
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel

# option actions understood by the Asterisk ``pet-ivr`` context
IVR_ACTIONS = ("dial", "announcement", "menu", "voicemail", "hangup")


def _audio_path(instance, filename):
    return f"ivr/{instance.extension.event.slug}/{instance.extension.number}/{filename}"


class Announcement(TimeStampedModel):
    extension = models.OneToOneField("extensions.Extension", on_delete=models.CASCADE, related_name="announcement")
    audio = models.FileField(upload_to=_audio_path, null=True, blank=True)
    tts_text = models.TextField(blank=True, help_text=_("Spoken when no audio file is uploaded."))
    language = models.CharField(max_length=8, default="en")
    loop = models.BooleanField(default=False, help_text=_("Repeat until the caller hangs up."))

    def __str__(self):
        return f"announcement {self.extension.number}"

    @property
    def event(self):
        return self.extension.event


class IVRMenu(TimeStampedModel):
    extension = models.OneToOneField("extensions.Extension", on_delete=models.CASCADE, related_name="ivr_menu")
    prompt_audio = models.FileField(upload_to=_audio_path, null=True, blank=True)
    prompt_tts = models.TextField(blank=True)
    language = models.CharField(max_length=8, default="en")
    timeout = models.PositiveSmallIntegerField(default=5, help_text=_("Seconds to wait for a digit."))
    invalid_retries = models.PositiveSmallIntegerField(default=3)
    # [{"digit": "1", "action": "dial", "target": "4242", "label": "Bar"}, ...]
    options = models.JSONField(default=list, blank=True)

    def __str__(self):
        return f"ivr {self.extension.number}"

    @property
    def event(self):
        return self.extension.event


class FunService(TimeStampedModel):
    class Kind(models.TextChoices):
        ECHO = "echo", _("Echo test")
        TIME = "time", _("Speaking clock")
        MOH = "moh", _("Music on hold")
        SONG = "song", _("Dial-a-song")

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="fun_services")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    extension = models.OneToOneField("extensions.Extension", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="fun_service")
    audio = models.FileField(upload_to="ivr/songs/", null=True, blank=True, help_text=_("Dial-a-song audio."))

    def __str__(self):
        return f"{self.kind} ({self.event.slug})"
