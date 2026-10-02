"""Extensions: a number bound to a type, an owner and per-event settings."""
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


class ExtensionType(models.TextChoices):
    DECT = "dect", _("DECT handset")
    SIP = "sip", _("SIP endpoint")
    GSM = "gsm", _("GSM (local cell network)")
    WEBRTC = "webrtc", _("WebRTC browser softphone")
    ANALOG = "analog", _("Analog via ATA")
    GROUP = "group", _("Call group / hunt group")
    ANNOUNCEMENT = "announcement", _("Announcement")
    IVR = "ivr", _("IVR menu")
    CONFERENCE = "conference", _("Conference room")
    VOICEMAIL = "voicemail", _("Voicemail box")
    APP = "app", _("Application / service")
    FEDERATION = "federation", _("Federation trunk (DIAL-VPN)")
    BREAKOUT = "breakout", _("PSTN breakout")
    TRUNK = "trunk", _("SIP trunk (number block)")


# Types that end users may pick in self-service (others are orga-only)
USER_SELECTABLE_TYPES = [
    ExtensionType.DECT, ExtensionType.SIP, ExtensionType.WEBRTC, ExtensionType.GSM,
    ExtensionType.ANALOG, ExtensionType.GROUP, ExtensionType.ANNOUNCEMENT,
    ExtensionType.IVR, ExtensionType.CONFERENCE, ExtensionType.TRUNK,
]

# Allowed number of wildcard digits of a trunk block (block sizes 10 / 100 / 1000)
TRUNK_BLOCK_DIGITS = (1, 2, 3)

# Types that ring physical/soft endpoints and therefore carry device bindings
ENDPOINT_TYPES = [
    ExtensionType.DECT, ExtensionType.SIP, ExtensionType.WEBRTC, ExtensionType.GSM,
    ExtensionType.ANALOG,
]


# Announcement / prompt language of an extension (Asterisk sound pack). "" = use the event default.
LANGUAGE_CHOICES = [("", _("Event default")), *settings.DIAL_PBX_LANGUAGES]


def _new_token():
    return secrets.token_urlsafe(24)


def ringback_processed_path(instance, filename):
    """Each processed tone lives in its own directory so Asterisk ``mode=files`` can point at it."""
    return f"ringback/processed/{instance.pk}/tone.wav"


class ExtensionQuerySet(models.QuerySet):
    def active(self):
        return self.filter(state=Extension.State.ACTIVE)

    def for_event(self, event):
        return self.filter(event=event)

    def public(self):
        return self.active().filter(in_phonebook=True)


class Extension(TimeStampedModel):
    class State(models.TextChoices):
        REQUESTED = "requested", _("Requested - awaiting approval")
        ACTIVE = "active", _("Active")
        SUSPENDED = "suspended", _("Suspended")
        REJECTED = "rejected", _("Rejected")
        EXPIRED = "expired", _("Expired")
        DELETED = "deleted", _("Deleted")

    class RingStrategy(models.TextChoices):
        PARALLEL = "parallel", _("Ring all devices at once")
        SERIAL = "serial", _("Ring devices one after another")

    class ForwardMode(models.TextChoices):
        OFF = "off", _("No forwarding")
        ALWAYS = "always", _("Always (do not ring my devices)")
        BUSY = "busy", _("When busy")
        NOANSWER = "noanswer", _("When unanswered")
        DELAYED = "delayed", _("Delayed (ring my devices first, then forward)")

    class DisplayMode(models.TextChoices):
        NUMBER_NAME = "number_name", _("Number and name (\"4242 Alice\")")
        NAME = "name", _("Name only")
        NUMBER = "number", _("Number only")

    class RingbackStatus(models.TextChoices):
        NONE = "none", _("No custom tone")
        PENDING = "pending", _("Processing")
        READY = "ready", _("Ready")
        FAILED = "failed", _("Failed")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="extensions")
    number = models.CharField(max_length=16, db_index=True)
    type = models.CharField(max_length=20, choices=ExtensionType.choices, default=ExtensionType.DECT)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="extensions",
    )
    state = models.CharField(max_length=20, choices=State.choices, default=State.REQUESTED, db_index=True)
    display_name = models.CharField(max_length=60, blank=True, help_text=_("Caller-ID name."))
    description = models.CharField(max_length=200, blank=True)
    location_hint = models.CharField(max_length=120, blank=True, help_text=_("e.g. 'Hackcenter, table 12'"))
    in_phonebook = models.BooleanField(default=True, verbose_name=_("Public phonebook entry"))
    language = models.CharField(
        max_length=8, blank=True, choices=LANGUAGE_CHOICES,
        help_text=_("Language of voicemail and system announcements played to callers of this extension."),
    )
    ring_strategy = models.CharField(max_length=10, choices=RingStrategy.choices, default=RingStrategy.PARALLEL)
    ring_timeout = models.PositiveSmallIntegerField(default=30)
    # Legacy free-text forwarding targets (orga may point at arbitrary numbers). Only used when
    # ``forward_mode`` is ``off``; the FK-based forwarding below takes precedence otherwise.
    forward_busy = models.CharField(max_length=16, blank=True)
    forward_noanswer = models.CharField(max_length=16, blank=True)
    forward_unconditional = models.CharField(max_length=16, blank=True)
    forward_mode = models.CharField(max_length=10, choices=ForwardMode.choices, default=ForwardMode.OFF)
    forward_delay = models.PositiveSmallIntegerField(
        default=10, help_text=_("Seconds to ring your own devices before forwarding (mode 'delayed')."),
    )
    forward_target = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="forwarded_from",
    )
    # Per-extension toggles (GURU3-style)
    call_waiting = models.BooleanField(
        default=True, help_text=_("Off: a second incoming call gets a busy signal instead of knocking."),
    )
    display_mode = models.CharField(
        max_length=12, choices=DisplayMode.choices, default=DisplayMode.NUMBER_NAME,
        help_text=_("What the called party sees as your caller ID."),
    )
    dect_encryption = models.BooleanField(
        default=False, help_text=_("Encrypt the DECT air interface for handsets on this extension."),
    )
    # Custom ringback tone: the caller hears this instead of the standard ring while your devices ring
    ringback_tone = models.FileField(upload_to="ringback/", blank=True, null=True)
    ringback_tone_processed = models.FileField(upload_to=ringback_processed_path, blank=True, null=True,
                                              editable=False)
    ringback_tone_status = models.CharField(max_length=10, choices=RingbackStatus.choices,
                                           default=RingbackStatus.NONE, editable=False)
    ringback_tone_error = models.CharField(max_length=300, blank=True, editable=False)
    allow_callback = models.BooleanField(default=True, help_text=_("Allow CCBS/CCNR on this extension."))
    priority = models.PositiveSmallIntegerField(
        default=0, help_text=_("0 = normal. Higher values can preempt (emergency/orga)."),
    )
    is_temporary = models.BooleanField(default=False, help_text=_("Guest extension; expires automatically."))
    expires_at = models.DateTimeField(null=True, blank=True)
    claim_token = models.CharField(max_length=64, blank=True, db_index=True,
                                   help_text=_("QR/deeplink token to claim a guest extension."))
    dect_claim_code = models.CharField(
        max_length=8, blank=True, db_index=True,
        help_text=_("Digits the owner dials (after the plan's DECT claim number) from any subscribed handset "
                    "to bind that handset to this extension."),
    )
    announcement_record_code = models.CharField(
        max_length=8, blank=True, db_index=True,
        help_text=_("Digits the owner dials (after the plan's announcement recording number) to record this "
                    "announcement by phone."),
    )
    # Type-specific configuration (announcement audio, IVR tree, conference PIN...)
    config = models.JSONField(default=dict, blank=True)
    # Moderation
    request_note = models.TextField(blank=True)
    moderation_note = models.TextField(blank=True)
    moderated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    moderated_at = models.DateTimeField(null=True, blank=True)
    # Provisioning sync
    provisioned_at = models.DateTimeField(null=True, blank=True)
    provision_error = models.TextField(blank=True)
    # Origin (for "re-request my number from last year")
    ported_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="ported_to",
    )

    objects = ExtensionQuerySet.as_manager()

    class Meta:
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "number"],
                condition=models.Q(state__in=["requested", "active", "suspended"]),
                name="unique_live_number_per_event",
            )
        ]
        permissions = [("moderate_extension", "Can approve/reject extensions")]

    def __str__(self):
        return f"{self.number} ({self.event.slug})"

    @property
    def is_active(self):
        return self.state == self.State.ACTIVE

    @property
    def is_endpoint(self):
        return self.type in ENDPOINT_TYPES

    @property
    def is_trunk(self):
        return self.type == ExtensionType.TRUNK

    @property
    def accepts_devices(self):
        """Endpoint extensions ring devices; a trunk carries exactly one SIP account (the remote PBX)."""
        return self.is_endpoint or self.is_trunk

    # --- trunk blocks ------------------------------------------------------
    @property
    def block_digits(self) -> int:
        """Trailing wildcard digits of a trunk block (``config["block_digits"]``); 0 for non-trunks."""
        if not self.is_trunk:
            return 0
        try:
            return int((self.config or {}).get("block_digits") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def block_size(self) -> int:
        return 10 ** self.block_digits

    @property
    def block_prefix(self) -> str:
        """Digits shared by every number of the block (``47`` for ``4700``/2)."""
        d = self.block_digits
        return self.number[:-d] if d else self.number

    @property
    def block_pattern(self) -> str:
        """Asterisk pattern matching the whole block (``_47XX``)."""
        return f"_{self.block_prefix}{'X' * self.block_digits}"

    def block_range(self) -> tuple[str, str]:
        """``(first, last)`` number of the block - ``("4700", "4799")``."""
        return self.number, self.block_prefix + "9" * self.block_digits

    def covers(self, number: str) -> bool:
        """True if ``number`` lies inside this trunk's block (the base itself included)."""
        number = (number or "").strip()
        return (self.is_trunk and len(number) == len(self.number) and number.isdigit()
                and number.startswith(self.block_prefix))

    @property
    def number_label(self) -> str:
        """``4700–4799`` for trunk blocks, the plain number otherwise (lists, phonebook)."""
        if self.is_trunk and self.block_digits:
            lo, hi = self.block_range()
            return f"{lo}–{hi}"
        return self.number

    @property
    def caller_id_name(self):
        return self.display_name or (self.owner.username if self.owner else self.number)

    @property
    def sip_uri(self):
        domain = self.event.sip_domain or settings.ASTERISK["SIP_DOMAIN"]
        return f"sip:{self.number}@{domain}"

    @property
    def is_forwarding(self):
        return self.forward_mode != self.ForwardMode.OFF and self.forward_target_id is not None

    @property
    def forward_target_number(self):
        """Number of the FK target (``""`` when unset)."""
        return self.forward_target.number if self.forward_target_id else ""

    @property
    def is_forward_target(self):
        """True if any live extension currently forwards to this one."""
        return self.live_forwarders().exists()

    def live_forwarders(self):
        return self.forwarded_from.filter(
            state__in=[self.State.REQUESTED, self.State.ACTIVE, self.State.SUSPENDED],
        ).exclude(forward_mode=self.ForwardMode.OFF)

    @property
    def has_ringback_tone(self):
        return self.ringback_tone_status == self.RingbackStatus.READY and bool(self.ringback_tone_processed)

    @property
    def ringback_class(self):
        """Asterisk music-on-hold class name used for ``Dial(...,m(<class>))``."""
        return f"dial-{self.event.slug}-{self.number}"

    @property
    def caller_id_display(self):
        """Caller-ID *name* this extension presents, according to ``display_mode``."""
        if self.display_mode == self.DisplayMode.NAME:
            return self.caller_id_name
        if self.display_mode == self.DisplayMode.NUMBER:
            return self.number
        return f"{self.number} {self.caller_id_name}"

    def issue_claim_token(self):
        self.claim_token = secrets.token_urlsafe(24)
        return self.claim_token

    def _unique_dial_code(self, field: str) -> str:
        """Random digit code (``DIAL_DECT_CLAIM_CODE_LENGTH``) unique for ``field`` among the event's live
        extensions."""
        length = getattr(settings, "DIAL_DECT_CLAIM_CODE_LENGTH", 6)
        live = Extension.objects.filter(event_id=self.event_id, state__in=["requested", "active", "suspended"])
        for _attempt in range(50):
            code = "".join(secrets.choice("0123456789") for _ in range(length))
            if not live.filter(**{field: code}).exclude(pk=self.pk).exists():
                break
        return code

    def issue_dect_claim_code(self, save=True) -> str:
        """New random claim code, unique among this event's live extensions."""
        code = self._unique_dial_code("dect_claim_code")
        self.dect_claim_code = code
        if save:
            self.save(update_fields=["dect_claim_code", "updated_at"])
        return code

    def issue_record_code(self, save=True) -> str:
        """New random announcement recording code, unique among this event's live extensions."""
        code = self._unique_dial_code("announcement_record_code")
        self.announcement_record_code = code
        if save:
            self.save(update_fields=["announcement_record_code", "updated_at"])
        return code

    def mark_expired(self):
        self.state = self.State.EXPIRED
        self.save(update_fields=["state", "updated_at"])


class ExtensionRequest(TimeStampedModel):
    """Waitlist entry for a number that is currently taken."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="waitlist")
    number = models.CharField(max_length=16)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="waitlist")
    type = models.CharField(max_length=20, choices=ExtensionType.choices, default=ExtensionType.DECT)
    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("event", "number", "user")]
        ordering = ["created_at"]

    def __str__(self):
        return f"waitlist {self.number} for {self.user}"


class ExtensionTransfer(TimeStampedModel):
    """Pending transfer of an extension to another user (must be accepted)."""

    extension = models.ForeignKey(Extension, on_delete=models.CASCADE, related_name="transfers")
    from_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    to_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    token = models.CharField(max_length=64, unique=True, default=_new_token)
    accepted_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timezone.timedelta(days=2)
        super().save(*args, **kwargs)

    @property
    def is_open(self):
        return self.accepted_at is None and self.expires_at > timezone.now()
