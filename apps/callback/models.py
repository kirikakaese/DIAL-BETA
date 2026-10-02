"""Callbacks (CCBS/CCNR), scheduled/wake-up calls and the test ringback service."""
from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


def default_expiry():
    ttl = getattr(settings, "PET_CALLBACK_DEFAULT_TTL_MINUTES", 30)
    return timezone.now() + dt.timedelta(minutes=ttl)


def default_ringback_delay():
    return getattr(settings, "PET_TEST_RINGBACK_DELAY_SECONDS", 10)


def announcement_upload_to(instance, filename):
    return f"callback/announcements/{instance.event.slug}/{filename}"


class CallbackRequest(models.Model):
    """A "call me back when X is free / has been active again" request."""

    class Kind(models.TextChoices):
        CCBS = "ccbs", _("Completion of calls to busy subscriber")
        CCNR = "ccnr", _("Completion of calls on no reply")

    class State(models.TextChoices):
        PENDING = "pending", _("Pending")
        DIALING = "dialing", _("Dialing")
        COMPLETED = "completed", _("Completed")
        CANCELLED = "cancelled", _("Cancelled")
        EXPIRED = "expired", _("Expired")
        FAILED = "failed", _("Failed")

    MAX_ATTEMPTS = 3
    OPEN_STATES = (State.PENDING, State.DIALING)

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="callback_requests")
    kind = models.CharField(max_length=4, choices=Kind.choices, default=Kind.CCBS)
    requester = models.ForeignKey("extensions.Extension", on_delete=models.CASCADE,
                                  related_name="callback_requests_made")
    target = models.ForeignKey("extensions.Extension", on_delete=models.CASCADE,
                               related_name="callback_requests_received")
    requester_number = models.CharField(max_length=16)
    target_number = models.CharField(max_length=16, db_index=True)
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=default_expiry)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    channel_id = models.CharField(max_length=120, blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["state", "next_attempt_at"], name="cb_req_state_next_idx")]
        verbose_name = _("callback request")
        verbose_name_plural = _("callback requests")

    def __str__(self):
        return f"{self.get_kind_display()} {self.requester_number} -> {self.target_number} [{self.state}]"

    @property
    def is_open(self):
        return self.state in self.OPEN_STATES

    @property
    def is_expired(self):
        return self.expires_at <= timezone.now()


class ScheduledCall(models.Model):
    """Wake-up / scheduled call to one extension, optionally repeating daily."""

    class Repeat(models.TextChoices):
        ONCE = "once", _("Once")
        DAILY = "daily", _("Daily")

    class State(models.TextChoices):
        SCHEDULED = "scheduled", _("Scheduled")
        DIALING = "dialing", _("Dialing")
        ANSWERED = "answered", _("Answered")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")

    class Announcement(models.TextChoices):
        DEFAULT = "default", _("Default wake-up announcement")
        CUSTOM = "custom", _("Custom announcement")

    OPEN_STATES = (State.SCHEDULED, State.DIALING)

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="scheduled_calls")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="scheduled_calls")
    extension = models.ForeignKey("extensions.Extension", on_delete=models.CASCADE, related_name="scheduled_calls")
    scheduled_for = models.DateTimeField(db_index=True)
    repeat = models.CharField(max_length=5, choices=Repeat.choices, default=Repeat.ONCE)
    max_retries = models.PositiveSmallIntegerField(default=3)
    retry_interval_minutes = models.PositiveSmallIntegerField(default=5)
    attempts = models.PositiveSmallIntegerField(default=0)
    state = models.CharField(max_length=10, choices=State.choices, default=State.SCHEDULED, db_index=True)
    announcement = models.CharField(max_length=8, choices=Announcement.choices, default=Announcement.DEFAULT)
    announcement_text = models.CharField(max_length=300, blank=True, help_text=_("Spoken via TTS."))
    announcement_file = models.FileField(upload_to=announcement_upload_to, blank=True, null=True)
    last_result = models.CharField(max_length=200, blank=True)
    channel_id = models.CharField(max_length=120, blank=True)
    snooze_minutes = models.PositiveSmallIntegerField(default=9)
    # Retry bookkeeping (``scheduled_for`` keeps the user-visible time; this is the next dial time)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["scheduled_for"]
        indexes = [models.Index(fields=["state", "next_attempt_at"], name="cb_sched_state_next_idx")]
        verbose_name = _("scheduled call")
        verbose_name_plural = _("scheduled calls")

    def __str__(self):
        return (f"{self.get_repeat_display()} call to {self.extension.number} "
                f"at {self.scheduled_for:%Y-%m-%d %H:%M} [{self.state}]")

    def save(self, *args, **kwargs):
        if self.next_attempt_at is None and self.state == self.State.SCHEDULED:
            self.next_attempt_at = self.scheduled_for
        super().save(*args, **kwargs)

    @property
    def is_open(self):
        return self.state in self.OPEN_STATES

    @property
    def announcement_value(self) -> str:
        """What the dialplan gets as ``PET_ANNOUNCEMENT``: a sound file path, TTS text or ''."""
        if self.announcement != self.Announcement.CUSTOM:
            return ""
        if self.announcement_file:
            if hasattr(self.announcement_file, "path"):
                return self.announcement_file.path
            return self.announcement_file.name
        return self.announcement_text or ""


class TestRingback(models.Model):
    """User dials the ringback service; PET calls back after ``delay_seconds``."""

    class State(models.TextChoices):
        SCHEDULED = "scheduled", _("Scheduled")
        DIALING = "dialing", _("Dialing")
        DELIVERED = "delivered", _("Delivered")
        FAILED = "failed", _("Failed")

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="test_ringbacks")
    extension = models.ForeignKey("extensions.Extension", null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="test_ringbacks")
    caller_number = models.CharField(max_length=16)
    requested_at = models.DateTimeField(auto_now_add=True)
    delay_seconds = models.PositiveSmallIntegerField(default=default_ringback_delay)
    state = models.CharField(max_length=10, choices=State.choices, default=State.SCHEDULED, db_index=True)
    channel_id = models.CharField(max_length=120, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        indexes = [models.Index(fields=["state", "next_attempt_at"], name="cb_ringback_state_next_idx")]
        verbose_name = _("test ringback")
        verbose_name_plural = _("test ringbacks")

    def __str__(self):
        return f"ringback {self.caller_number} in {self.delay_seconds}s [{self.state}]"
