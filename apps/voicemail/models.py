"""Voicemail: DIAL-side mailboxes and messages mirrored from Asterisk's app_voicemail.

Asterisk records the message and calls the ``voicemail`` PBX hook (``externnotify``); DIAL copies the
audio into ``MEDIA_ROOT``, updates MWI on PBX + DECT and optionally e-mails the message.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


def _greeting_path(instance, filename):
    return f"voicemail/{instance.event.slug}/{instance.extension.number}/greeting-{filename}"


def _message_path(instance, filename):
    mb = instance.mailbox
    return f"voicemail/{mb.event.slug}/{mb.extension.number}/{filename}"


class Mailbox(TimeStampedModel):
    """One voicemail box per extension.

    ``pin`` is stored in plain text on purpose: Asterisk's ``voicemail_users`` realtime table needs the
    clear-text PIN (``password`` column) and the boxes only live for the duration of an event. Treat the
    DIAL database as sensitive accordingly.
    """

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="mailboxes")
    extension = models.OneToOneField("extensions.Extension", on_delete=models.CASCADE, related_name="mailbox")
    pin = models.CharField(max_length=6, help_text=_("4-6 digits, entered on the handset to listen to messages."))
    email_delivery = models.BooleanField(default=False, verbose_name=_("Send new messages by e-mail"))
    email = models.EmailField(blank=True, help_text=_("Overrides the account e-mail address."))
    greeting = models.FileField(upload_to=_greeting_path, blank=True, null=True)
    max_messages = models.PositiveSmallIntegerField(default=50)
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["extension__number"]
        verbose_name = _("mailbox")
        verbose_name_plural = _("mailboxes")

    def __str__(self):
        return f"mailbox {self.extension.number}@{self.event.slug}"

    @property
    def number(self) -> str:
        return self.extension.number

    @property
    def owner(self):
        return self.extension.owner

    @property
    def delivery_address(self) -> str:
        if self.email:
            return self.email
        return self.owner.email if self.owner else ""

    def unread_count(self) -> int:
        return self.messages.filter(is_read=False).count()


class Message(TimeStampedModel):
    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="messages")
    caller_number = models.CharField(max_length=32, blank=True)
    caller_name = models.CharField(max_length=80, blank=True)
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    audio = models.FileField(upload_to=_message_path, blank=True, null=True)
    is_read = models.BooleanField(default=False)
    transcript = models.TextField(blank=True)
    asterisk_msg_id = models.CharField(max_length=255, blank=True, db_index=True,
                                       help_text=_("Original spool path / msg id, used for de-duplication."))

    class Meta:
        ordering = ["-received_at"]
        verbose_name = _("voicemail message")

    def __str__(self):
        return f"{self.received_at:%Y-%m-%d %H:%M} from {self.caller_number or '?'} -> {self.mailbox.number}"

    @property
    def has_audio(self) -> bool:
        return bool(self.audio and self.audio.name)

    @property
    def content_type(self) -> str:
        name = (self.audio.name if self.has_audio else "").lower()
        for ext, ctype in ((".wav", "audio/wav"), (".mp3", "audio/mpeg"), (".ogg", "audio/ogg"),
                           (".gsm", "audio/x-gsm"), (".wav49", "audio/wav")):
            if name.endswith(ext):
                return ctype
        return "application/octet-stream"


class MessageDelivery(models.Model):
    """Log of outbound notifications for a message (e-mail for now)."""

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="deliveries")
    kind = models.CharField(max_length=10, default="email")
    recipient = models.CharField(max_length=200, blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    ok = models.BooleanField(default=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.kind} -> {self.recipient} ({'ok' if self.ok else 'failed'})"


def default_from_email() -> str:
    return getattr(settings, "DIAL_VOICEMAIL_FROM_EMAIL", None) or settings.DEFAULT_FROM_EMAIL
