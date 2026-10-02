"""DECT infrastructure models: RFPs (base stations), sync clusters, venue maps, alerts.

State is mirrored from the DECT system (OMM) by ``apps.dect.tasks.poll_infrastructure``;
positions/labels for the coverage map are maintained by orga in PET.
"""
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


class VenueMap(TimeStampedModel):
    """An uploaded floor plan / site map on which RFPs are placed."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="venue_maps")
    name = models.CharField(max_length=80)
    image = models.ImageField(upload_to="venue_maps/")
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.event.slug}: {self.name}"


class SyncCluster(TimeStampedModel):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="sync_clusters")
    cluster_id = models.CharField(max_length=16)
    name = models.CharField(max_length=80, blank=True)

    class Meta:
        unique_together = [("event", "cluster_id")]

    def __str__(self):
        return self.name or f"Cluster {self.cluster_id}"

    @property
    def health(self) -> str:
        rfps = list(self.rfps.filter(is_active=True))
        if not rfps:
            return "empty"
        down = [r for r in rfps if not r.connected]
        unsynced = [r for r in rfps if r.connected and not r.synced]
        if len(down) == len(rfps):
            return "down"
        if down or unsynced:
            return "degraded"
        return "ok"


class RFP(TimeStampedModel):
    """Radio Fixed Part (DECT base station)."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="rfps")
    omm_id = models.CharField(max_length=32, db_index=True)
    name = models.CharField(max_length=80)
    mac_address = models.CharField(max_length=17, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    location = models.CharField(max_length=120, blank=True)
    cluster = models.ForeignKey(SyncCluster, null=True, blank=True, on_delete=models.SET_NULL, related_name="rfps")
    sync_source = models.CharField(max_length=32, blank=True)
    is_active = models.BooleanField(default=True)
    connected = models.BooleanField(default=False)
    synced = models.BooleanField(default=False)
    last_state_change = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    active_calls = models.PositiveSmallIntegerField(default=0)
    # Coverage map position (percent of image width/height)
    venue_map = models.ForeignKey(VenueMap, null=True, blank=True, on_delete=models.SET_NULL, related_name="rfps")
    pos_x = models.FloatField(null=True, blank=True)
    pos_y = models.FloatField(null=True, blank=True)
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = [("event", "omm_id")]
        ordering = ["name"]
        verbose_name = "RFP"
        verbose_name_plural = "RFPs"

    def __str__(self):
        return self.name

    @property
    def status(self) -> str:
        if not self.is_active:
            return "inactive"
        if not self.connected:
            return "down"
        if not self.synced:
            return "unsynced"
        return "up"

    @property
    def handset_count(self):
        return self.handsets.count()


class RFPStatusSample(models.Model):
    """Time series of RFP status/load for heatmaps and erlang statistics."""

    rfp = models.ForeignKey(RFP, on_delete=models.CASCADE, related_name="samples")
    at = models.DateTimeField(db_index=True)
    connected = models.BooleanField()
    synced = models.BooleanField()
    active_calls = models.PositiveSmallIntegerField(default=0)
    handsets = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-at"]


class Alert(TimeStampedModel):
    class Severity(models.TextChoices):
        INFO = "info", _("Info")
        WARNING = "warning", _("Warning")
        CRITICAL = "critical", _("Critical")

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="alerts")
    severity = models.CharField(max_length=10, choices=Severity.choices)
    kind = models.CharField(max_length=40, db_index=True)  # rfp.down, rfp.up, sync.degraded, omm.unreachable
    rfp = models.ForeignKey(RFP, null=True, blank=True, on_delete=models.SET_NULL, related_name="alerts")
    message = models.CharField(max_length=300)
    resolved_at = models.DateTimeField(null=True, blank=True)
    notified = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.severity}] {self.message}"


class SiteSurveyLog(models.Model):
    """Walk-test log: which RFP served a handset when the survey number was dialed."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="survey_logs")
    device = models.ForeignKey("devices.Device", null=True, blank=True, on_delete=models.SET_NULL)
    rfp = models.ForeignKey(RFP, null=True, blank=True, on_delete=models.SET_NULL)
    rssi = models.SmallIntegerField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at"]


# --------------------------------------------------------------------------- per-event venue DECT

DECT_BACKEND_LABELS = {
    "omm": "Mitel SIP-DECT OMM (AXI)",
    "dummy": _("Simulator - no real DECT system"),
}


def dect_backend_choices():
    from django.conf import settings

    return [(k, DECT_BACKEND_LABELS.get(k, k)) for k in getattr(settings, "PET_DECT_BACKENDS", {})]


class DECTConnection(TimeStampedModel):
    """How PET reaches *this event's* DECT system (OMM) at the venue.

    One row per event; events without one fall back to the server-wide ``OMM`` settings from ``.env``.
    """

    event = models.OneToOneField("events.Event", on_delete=models.CASCADE, related_name="dect_connection")
    backend = models.CharField(max_length=40, default="omm", help_text=_("Adapter key from PET_DECT_BACKENDS."))
    host = models.CharField(_("OMM host"), max_length=200, blank=True, help_text=_("IP or hostname of the OMM."))
    port = models.PositiveIntegerField(_("AXI port"), default=12622)
    user = models.CharField(_("AXI user"), max_length=64, blank=True, default="omm")
    password = models.CharField(_("AXI password"), max_length=128, blank=True)
    verify_tls = models.BooleanField(_("Verify TLS certificate"), default=False)
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name = _("DECT connection")

    def __str__(self):
        return f"{self.event.slug}: {self.backend} {self.host or '-'}"

    @property
    def backend_path(self) -> str:
        from django.conf import settings

        try:
            return settings.PET_DECT_BACKENDS[self.backend]
        except KeyError as exc:
            raise ValueError(f"Unknown DECT backend {self.backend!r}") from exc

    @property
    def backend_label(self) -> str:
        return str(DECT_BACKEND_LABELS.get(self.backend, self.backend))

    def config(self) -> dict:
        """``settings.OMM``-shaped dict."""
        return {"HOST": self.host, "PORT": self.port or 12622, "USER": self.user or "omm",
                "PASSWORD": self.password, "VERIFY_TLS": self.verify_tls}
