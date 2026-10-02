from django.contrib import admin

from .models import BroadcastAnnouncement, EmergencyIncident, EmergencyTarget


@admin.register(EmergencyTarget)
class EmergencyTargetAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "number", "label", "destination_extension", "fallback_number", "priority")
    list_filter = ("event",)
    raw_id_fields = ("destination_extension",)


@admin.register(EmergencyIncident)
class EmergencyIncidentAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "number", "caller_number", "at", "handled_by", "resolved_at")
    list_filter = ("event", "number")
    raw_id_fields = ("caller_extension", "handled_by")
    date_hierarchy = "at"


@admin.register(BroadcastAnnouncement)
class BroadcastAnnouncementAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "sent_at", "sent_by", "group", "targets")
    list_filter = ("event",)
    raw_id_fields = ("sent_by", "group")
