from django.contrib import admin

from .models import CallRecord, ExtensionStat, HourlyStat


@admin.register(CallRecord)
class CallRecordAdmin(admin.ModelAdmin):
    list_display = ("started_at", "event", "src_number", "dst_number", "disposition", "duration", "billsec", "rfp")
    list_filter = ("event", "disposition", "src_type", "dst_type")
    search_fields = ("src_number", "dst_number", "uniqueid")
    date_hierarchy = "started_at"
    raw_id_fields = ("src_extension", "dst_extension", "rfp")
    readonly_fields = ("created_at",)


@admin.register(HourlyStat)
class HourlyStatAdmin(admin.ModelAdmin):
    list_display = ("hour", "event", "calls", "answered", "total_billsec", "unique_callers", "updated_at")
    list_filter = ("event",)
    date_hierarchy = "hour"
    readonly_fields = ("updated_at",)


@admin.register(ExtensionStat)
class ExtensionStatAdmin(admin.ModelAdmin):
    list_display = ("date", "event", "extension", "inbound", "outbound", "answered", "total_billsec")
    list_filter = ("event",)
    search_fields = ("extension__number",)
    raw_id_fields = ("extension",)
    date_hierarchy = "date"
