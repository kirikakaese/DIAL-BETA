"""Call statistics: PET's privacy-aware CDR copy plus hourly / per-extension aggregates.

``CallRecord`` rows are only written when ``event.cdr_aggregate_only`` is off and are purged by the
retention task; ``HourlyStat`` / ``ExtensionStat`` carry no per-call data and are kept.
"""
from django.db import models
from django.utils.translation import gettext_lazy as _


class CallRecord(models.Model):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="call_records")
    started_at = models.DateTimeField(db_index=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    src_number = models.CharField(max_length=32, blank=True, db_index=True)
    dst_number = models.CharField(max_length=32, blank=True, db_index=True)
    src_extension = models.ForeignKey("extensions.Extension", null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="outbound_calls")
    dst_extension = models.ForeignKey("extensions.Extension", null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="inbound_calls")
    duration = models.PositiveIntegerField(default=0, help_text=_("Seconds from dial to hangup."))
    billsec = models.PositiveIntegerField(default=0, help_text=_("Seconds the call was connected."))
    disposition = models.CharField(max_length=20, blank=True, db_index=True)  # ANSWERED / NO ANSWER / BUSY / FAILED
    src_type = models.CharField(max_length=20, blank=True)
    dst_type = models.CharField(max_length=20, blank=True)
    rfp = models.ForeignKey("dect.RFP", null=True, blank=True, on_delete=models.SET_NULL, related_name="call_records",
                            help_text=_("RFP serving the (DECT) caller, if known."))
    uniqueid = models.CharField(max_length=150, unique=True)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = _("call record")

    def __str__(self):
        return f"{self.src_number} -> {self.dst_number} {self.started_at:%Y-%m-%d %H:%M} {self.disposition}"

    @property
    def answered(self) -> bool:
        return self.disposition == "ANSWERED"


class HourlyStat(models.Model):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="hourly_stats")
    hour = models.DateTimeField(db_index=True)
    calls = models.PositiveIntegerField(default=0)
    answered = models.PositiveIntegerField(default=0)
    total_billsec = models.PositiveIntegerField(default=0)
    unique_callers = models.PositiveIntegerField(default=0)
    by_type = models.JSONField(default=dict, blank=True)          # {"dect": calls, "group": calls, ...} (dst types)
    by_disposition = models.JSONField(default=dict, blank=True)   # {"ANSWERED": n, "NO ANSWER": n, ...}
    # by_rfp: {rfp_id: {"calls": n, "erlang": x, "erlang_dect": y}}
    by_rfp = models.JSONField(default=dict, blank=True)
    # salted short hashes of caller numbers seen this hour; only used to count unique callers incrementally
    caller_hashes = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("event", "hour")]
        ordering = ["hour"]
        verbose_name = _("hourly statistic")

    def __str__(self):
        return f"{self.event.slug} {self.hour:%Y-%m-%d %H}h: {self.calls} calls"


class ExtensionStat(models.Model):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="extension_stats")
    extension = models.ForeignKey("extensions.Extension", on_delete=models.CASCADE, related_name="stats")
    date = models.DateField(db_index=True)
    inbound = models.PositiveIntegerField(default=0)
    outbound = models.PositiveIntegerField(default=0)
    answered = models.PositiveIntegerField(default=0)
    total_billsec = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("event", "extension", "date")]
        ordering = ["-date"]
        verbose_name = _("extension statistic")

    def __str__(self):
        return f"{self.extension.number} {self.date}: in {self.inbound} / out {self.outbound}"
