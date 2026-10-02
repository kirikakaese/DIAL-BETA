from django.contrib import admin

from .models import CallbackRequest, ScheduledCall, TestRingback


@admin.register(CallbackRequest)
class CallbackRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "kind", "requester_number", "target_number", "state", "attempts",
                    "created_at", "expires_at", "next_attempt_at")
    list_filter = ("event", "kind", "state")
    search_fields = ("requester_number", "target_number", "channel_id", "note")
    raw_id_fields = ("requester", "target")
    readonly_fields = ("created_at", "last_attempt_at", "channel_id")
    date_hierarchy = "created_at"


@admin.register(ScheduledCall)
class ScheduledCallAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "extension", "owner", "scheduled_for", "repeat", "state", "attempts",
                    "next_attempt_at", "last_result")
    list_filter = ("event", "repeat", "state", "announcement")
    search_fields = ("extension__number", "owner__username", "owner__email", "channel_id")
    raw_id_fields = ("extension", "owner")
    readonly_fields = ("created_at", "updated_at", "channel_id")
    date_hierarchy = "scheduled_for"


@admin.register(TestRingback)
class TestRingbackAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "caller_number", "extension", "delay_seconds", "state", "requested_at")
    list_filter = ("event", "state")
    search_fields = ("caller_number", "channel_id")
    raw_id_fields = ("extension",)
    readonly_fields = ("requested_at", "channel_id")
