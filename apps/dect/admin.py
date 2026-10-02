from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import RFP, Alert, DECTConnection, RFPStatusSample, SiteSurveyLog, SyncCluster, VenueMap


@admin.register(DECTConnection)
class DECTConnectionAdmin(admin.ModelAdmin):
    """Per-event venue OMM. Orga normally edit this on /e/<slug>/pbx/; the admin is for support."""

    list_display = ("event", "backend", "host", "port", "user", "verify_tls", "updated_at")
    list_filter = ("backend", "verify_tls")
    search_fields = ("event__slug", "event__name", "host", "notes")
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("event",)
    fieldsets = (
        (None, {"fields": ("event", "backend", "notes")}),
        (_("AXI"), {"fields": ("host", "port", "user", "password", "verify_tls")}),
        (_("Timestamps"), {"fields": ("created_at", "updated_at")}),
    )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from apps.dect import reset_dect_cache

        reset_dect_cache()

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        from apps.dect import reset_dect_cache

        reset_dect_cache()


@admin.register(VenueMap)
class VenueMapAdmin(admin.ModelAdmin):
    list_display = ("name", "event", "width", "height", "is_default")
    list_filter = ("event",)
    search_fields = ("name",)


@admin.register(SyncCluster)
class SyncClusterAdmin(admin.ModelAdmin):
    list_display = ("__str__", "event", "cluster_id", "health")
    list_filter = ("event",)


@admin.register(RFP)
class RFPAdmin(admin.ModelAdmin):
    list_display = ("name", "event", "omm_id", "cluster", "status", "connected", "synced", "active_calls",
                    "last_seen_at", "is_active")
    list_filter = ("event", "connected", "synced", "is_active", "cluster")
    search_fields = ("name", "omm_id", "mac_address", "location")
    readonly_fields = ("connected", "synced", "last_state_change", "last_seen_at", "active_calls", "extra")


@admin.register(RFPStatusSample)
class RFPStatusSampleAdmin(admin.ModelAdmin):
    list_display = ("rfp", "at", "connected", "synced", "active_calls", "handsets")
    list_filter = ("rfp__event", "connected")
    date_hierarchy = "at"


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("created_at", "event", "severity", "kind", "rfp", "message", "resolved_at", "notified")
    list_filter = ("event", "severity", "kind", "notified")
    search_fields = ("message",)


@admin.register(SiteSurveyLog)
class SiteSurveyLogAdmin(admin.ModelAdmin):
    list_display = ("at", "event", "device", "rfp", "rssi", "note")
    list_filter = ("event",)
