"""Devices / endpoints.

A ``Device`` is a concrete endpoint that can ring: a DECT handset (IPEI), a
SIP endpoint (credentials), a WebRTC browser, a GSM handset, an analog ATA
port. Devices are bound to extensions via ``DeviceBinding`` so one number
can ring several devices (parallel or serial) and one SIP account can be
attached to several numbers.

New device classes plug in via ``apps.devices.endpoint_types``.
"""
import secrets
import string
import uuid

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel

ipei_validator = RegexValidator(r"^\d{13}$", _("IPEI must be 13 digits (5 digit EMC + 7 digit PSN + check)."))
emc_validator = RegexValidator(r"^\d{5}$", _("The EMC is the first 5 digits of an IPEI."))


class DeviceType(models.TextChoices):
    DECT = "dect", _("DECT handset")
    SIP = "sip", _("SIP endpoint")
    WEBRTC = "webrtc", _("WebRTC softphone")
    GSM = "gsm", _("GSM handset")
    ANALOG = "analog", _("Analog / ATA")


def generate_sip_password(length: int | None = None) -> str:
    length = length or settings.DIAL_SIP_PASSWORD_LENGTH
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_pin(length: int = 6) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(length))


# QR flavours a softphone can be onboarded with (``?client=`` of the QR view, keys of ``softphone_links``).
SOFTPHONE_CLIENTS = ("generic", "linphone", "acrobits")


class Device(TimeStampedModel):
    class State(models.TextChoices):
        NEW = "new", _("New")
        PENDING = "pending", _("Pending subscription")
        SUBSCRIBED = "subscribed", _("Subscribed / registered")
        OFFLINE = "offline", _("Offline")
        DISABLED = "disabled", _("Disabled")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="devices")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="devices")
    type = models.CharField(max_length=10, choices=DeviceType.choices)
    name = models.CharField(max_length=80, blank=True, help_text=_("Free-form label, e.g. 'my orange handset'"))
    state = models.CharField(max_length=12, choices=State.choices, default=State.NEW)
    # DECT
    ipei = models.CharField(max_length=13, blank=True, validators=[ipei_validator], db_index=True)
    subscription_pin = models.CharField(max_length=16, blank=True)
    subscription_pin_expires_at = models.DateTimeField(null=True, blank=True)
    omm_ppn = models.CharField(max_length=32, blank=True, help_text=_("Portable part number in the OMM."))
    omm_user_id = models.CharField(max_length=32, blank=True)
    last_seen_rfp = models.ForeignKey("dect.RFP", null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="handsets")
    last_seen_at = models.DateTimeField(null=True, blank=True)
    battery_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    rssi = models.SmallIntegerField(null=True, blank=True)
    handset_model = models.CharField(max_length=60, blank=True)
    uak = models.CharField(
        max_length=64, blank=True,
        help_text=_("User Authentication Key stored by the OMM; lets a handset re-subscribe without a new PIN."),
    )
    unclaimed = models.BooleanField(
        default=False, db_index=True,
        help_text=_("Pool handset: subscribed to the DECT network but not yet bound to an extension. "
                    "Its user dials the claim code of an extension to take it over."),
    )
    # SIP
    sip_username = models.CharField(max_length=64, blank=True, db_index=True)
    sip_password = models.CharField(max_length=128, blank=True)
    sip_password_rotated_at = models.DateTimeField(null=True, blank=True)
    sip_transport = models.CharField(max_length=8, default="udp", choices=[
        ("udp", "UDP"), ("tcp", "TCP"), ("tls", "TLS"), ("wss", "WSS"),
    ])
    sip_user_agent = models.CharField(max_length=120, blank=True)
    sip_contact = models.CharField(max_length=200, blank=True)
    sip_registered_at = models.DateTimeField(null=True, blank=True)
    mac_address = models.CharField(max_length=17, blank=True, db_index=True)
    provisioning_profile = models.ForeignKey(
        "ProvisioningProfile", null=True, blank=True, on_delete=models.SET_NULL, related_name="devices",
    )
    provisioning_token = models.CharField(max_length=64, blank=True, db_index=True)
    # GSM
    imsi = models.CharField(max_length=15, blank=True)
    msisdn = models.CharField(max_length=20, blank=True)
    gsm_2g = models.BooleanField(default=True, verbose_name="2G", help_text=_("SIM may attach to GSM/GPRS."))
    gsm_3g = models.BooleanField(default=False, verbose_name="3G", help_text=_("SIM may attach to UMTS."))
    gsm_4g = models.BooleanField(default=False, verbose_name="4G", help_text=_("SIM may attach to LTE."))
    gsm_5g = models.BooleanField(default=False, verbose_name="5G", help_text=_("SIM may attach to NR."))
    gsm_register_token = models.CharField(
        max_length=32, blank=True, db_index=True,
        help_text=_("One-time code the user dials/texts on the GSM network to link the SIM to this device."),
    )
    gsm_registered_at = models.DateTimeField(null=True, blank=True)
    # misc
    config = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["event", "ipei"], condition=~models.Q(ipei=""),
                                    name="unique_ipei_per_event"),
            models.UniqueConstraint(fields=["sip_username"], condition=~models.Q(sip_username=""),
                                    name="unique_sip_username"),
        ]

    def __str__(self):
        ident = self.ipei or self.sip_username or self.imsi or str(self.pk)[:8]
        return f"{self.get_type_display()} {ident}"

    # --- SIP helpers -------------------------------------------------------
    def ensure_sip_credentials(self, save=True):
        if not self.sip_username:
            self.sip_username = f"{self.event.slug[:8]}-{secrets.token_hex(4)}"
        if not self.sip_password:
            self.sip_password = generate_sip_password()
            self.sip_password_rotated_at = timezone.now()
        if not self.provisioning_token:
            self.provisioning_token = secrets.token_urlsafe(24)
        if save:
            self.save()

    def rotate_sip_password(self, save=True):
        self.sip_password = generate_sip_password()
        self.sip_password_rotated_at = timezone.now()
        if save:
            self.save(update_fields=["sip_password", "sip_password_rotated_at", "updated_at"])

    @property
    def sip_domain(self):
        return self.event.sip_domain or settings.ASTERISK["SIP_DOMAIN"]

    @property
    def sip_uri(self):
        return f"sip:{self.sip_username}@{self.sip_domain}"

    @property
    def sip_port(self) -> int:
        return 5061 if self.sip_transport == "tls" else 5060

    @property
    def sip_display_name(self) -> str:
        """Caller-ID name of the primary extension, falling back to the SIP username."""
        ext = self.primary_extension
        return ext.caller_id_name if ext else self.sip_username

    def softphone_deeplink(self) -> str:
        """Generic softphone deep link consumed by e.g. Linphone/Zoiper style clients."""
        return (f"sip:{self.sip_username}:{self.sip_password}@{self.sip_domain}"
                f";transport={self.sip_transport}")

    def softphone_links(self) -> dict[str, str]:
        """QR payloads per softphone family - all of them carry the credentials (or the token that serves them).

        * ``generic``  - ``sip:user:pass@domain;transport=…`` deep link.
        * ``linphone`` - ``linphone-config:<url>`` pointing at ``/prov/<token>/linphone.xml``.
        * ``acrobits`` - plain URL of ``/prov/<token>/acrobits.xml`` (Groundwire / Cloud Softphone / Acrobits).

        The provisioning-based entries are empty until the device has a ``provisioning_token``.
        """
        links = {"generic": self.softphone_deeplink(), "linphone": "", "acrobits": ""}
        if self.provisioning_token:
            base = f"{settings.DIAL_PUBLIC_URL.rstrip('/')}/prov/{self.provisioning_token}"
            links["linphone"] = f"linphone-config:{base}/linphone.xml"
            links["acrobits"] = f"{base}/acrobits.xml"
        return links

    # --- DECT helpers ------------------------------------------------------
    def issue_subscription_pin(self, ttl_minutes: int = 30, save=True):
        self.subscription_pin = generate_pin()
        self.subscription_pin_expires_at = timezone.now() + timezone.timedelta(minutes=ttl_minutes)
        self.state = self.State.PENDING
        if save:
            self.save()
        return self.subscription_pin

    @property
    def pin_valid(self):
        return bool(self.subscription_pin and self.subscription_pin_expires_at
                    and self.subscription_pin_expires_at > timezone.now())

    @property
    def emc(self) -> str:
        """Equipment Manufacturer Code: the first 5 digits of the IPEI."""
        digits = "".join(ch for ch in (self.ipei or "") if ch.isdigit())
        return digits[:5] if len(digits) >= 5 else ""

    @property
    def manufacturer(self):
        """``DECTManufacturer`` derived from the IPEI's EMC, or ``None`` when unknown."""
        from .services import vendor_for_ipei

        return vendor_for_ipei(self.ipei)

    # --- GSM helpers -------------------------------------------------------
    def issue_gsm_register_token(self, save=True) -> str:
        self.gsm_register_token = generate_pin()
        if save:
            self.save(update_fields=["gsm_register_token", "updated_at"])
        return self.gsm_register_token

    @property
    def gsm_generations(self) -> list[str]:
        return [label for flag, label in ((self.gsm_2g, "2G"), (self.gsm_3g, "3G"), (self.gsm_4g, "4G"),
                                          (self.gsm_5g, "5G")) if flag]

    # --- provisioning helpers ---------------------------------------------
    @property
    def mac_plain(self) -> str:
        """MAC address without separators, lowercase (what most phones put into config file names)."""
        return "".join(ch for ch in (self.mac_address or "") if ch.isalnum()).lower()

    @property
    def provisioning_filename(self) -> str:
        if self.provisioning_profile_id is None:
            return ""
        pattern = self.provisioning_profile.filename_pattern or "{mac}.cfg"
        return pattern.replace("{mac}", self.mac_plain).replace("{MAC}", self.mac_plain.upper())

    @property
    def provisioning_url(self) -> str:
        """Absolute URL a phone fetches its config from (``/prov/<token>/<filename>``)."""
        if not self.provisioning_token or self.provisioning_profile_id is None:
            return ""
        return f"{settings.DIAL_PUBLIC_URL.rstrip('/')}/prov/{self.provisioning_token}/{self.provisioning_filename}"

    @property
    def primary_extension(self):
        b = self.bindings.select_related("extension").order_by("priority").first()
        return b.extension if b else None


class DECTManufacturer(TimeStampedModel):
    """Vendor behind an IPEI's Equipment Manufacturer Code (first 5 digits).

    Seeded from ``apps.devices.dect_vendors`` (``source=builtin``) and crowdsourced by users who
    know their handset (``source=user``).
    """

    class Source(models.TextChoices):
        BUILTIN = "builtin", _("Built-in")
        USER = "user", _("User contributed")

    emc = models.CharField(max_length=5, unique=True, validators=[emc_validator], verbose_name="EMC")
    name = models.CharField(max_length=80)
    models_hint = models.TextField(blank=True, help_text=_("Known handset models with this EMC, e.g. '612d, 622d'."))
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.USER)

    class Meta:
        ordering = ["emc"]
        verbose_name = _("DECT manufacturer")

    def __str__(self):
        return f"{self.emc} {self.name}"


class DeviceBinding(TimeStampedModel):
    """Attach a device to an extension. Multiple bindings = multi-device extension."""

    extension = models.ForeignKey("extensions.Extension", on_delete=models.CASCADE, related_name="bindings")
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="bindings")
    priority = models.PositiveSmallIntegerField(default=0, help_text=_("Serial ring order; lower first."))
    ring_delay = models.PositiveSmallIntegerField(default=0, help_text=_("Seconds to wait before ringing."))
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [("extension", "device")]
        ordering = ["priority"]

    def __str__(self):
        return f"{self.extension.number} -> {self.device}"


class ProvisioningProfile(TimeStampedModel):
    """Autoprovisioning template for common hard phones."""

    class Vendor(models.TextChoices):
        SNOM = "snom", "Snom"
        YEALINK = "yealink", "Yealink"
        GRANDSTREAM = "grandstream", "Grandstream"
        CISCO = "cisco", "Cisco"
        GENERIC = "generic", "Generic"

    event = models.ForeignKey("events.Event", null=True, blank=True, on_delete=models.CASCADE,
                              related_name="provisioning_profiles")
    name = models.CharField(max_length=80)
    vendor = models.CharField(max_length=20, choices=Vendor.choices)
    template = models.TextField(help_text=_(
        "Django template; context: device, extension, event, sip_server, sip_port, transport."))
    content_type = models.CharField(max_length=60, default="text/plain")
    filename_pattern = models.CharField(max_length=80, default="{mac}.cfg")

    def __str__(self):
        return f"{self.name} ({self.vendor})"

    def render(self, device: Device) -> str:
        """Render the template. Context: ``device``, ``extension``, ``event``, ``sip_server``, ``sip_port``,
        ``transport``, ``display_name`` and ``phonebook_url`` (``/prov/<token>/phonebook.xml``, empty when the
        event's remote directory is off)."""
        from django.template import Context, Template

        from apps.phonebook.remote import prov_directory_url

        ext = device.primary_extension
        ctx = Context({
            "device": device, "extension": ext, "event": device.event,
            "sip_server": device.sip_domain, "sip_port": device.sip_port,
            "transport": device.sip_transport,
            "display_name": ext.caller_id_name if ext else device.sip_username,
            "phonebook_url": prov_directory_url(device),
        })
        return Template(self.template).render(ctx)
