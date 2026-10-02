"""Federation (PET-VPN): SIP/TLS trunks between PET instances and a public directory cache."""
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


class FederationPeer(TimeStampedModel):
    class State(models.TextChoices):
        PENDING = "pending", _("Pending")
        ACTIVE = "active", _("Active")
        DISABLED = "disabled", _("Disabled")

    class Transport(models.TextChoices):
        TLS = "tls", "TLS"
        TCP = "tcp", "TCP"
        UDP = "udp", "UDP"
        WSS = "wss", "WSS"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="federation_peers")
    name = models.CharField(max_length=80)
    remote_prefix = models.CharField(max_length=8, help_text=_("Digits users dial first to reach this peer."))
    sip_host = models.CharField(max_length=200)
    sip_port = models.PositiveIntegerField(default=5061)
    transport = models.CharField(max_length=4, choices=Transport.choices, default=Transport.TLS)
    srtp = models.BooleanField(default=True)
    auth_user = models.CharField(max_length=80, blank=True)
    auth_password = models.CharField(max_length=128, blank=True)
    remote_event_name = models.CharField(max_length=120, blank=True)
    directory_url = models.URLField(blank=True)
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING, db_index=True)
    last_seen = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["remote_prefix"]
        unique_together = [("event", "remote_prefix")]

    def __str__(self):
        return f"{self.name} ({self.remote_prefix}→{self.sip_host})"

    @property
    def endpoint_name(self):
        return f"fed-{self.pk}"


class FederationDirectoryEntry(TimeStampedModel):
    """Cache of entries fetched from other instances' ``/api/v1/federation/directory/``."""

    instance_url = models.URLField()
    event_name = models.CharField(max_length=120)
    event_slug = models.SlugField(blank=True)
    dial_prefix = models.CharField(max_length=8, blank=True)
    sip_host = models.CharField(max_length=200, blank=True)
    sip_port = models.PositiveIntegerField(default=5061)
    contact = models.CharField(max_length=200, blank=True)
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["event_name"]
        unique_together = [("instance_url", "event_slug")]

    def __str__(self):
        return f"{self.event_name} @ {self.instance_url}"
