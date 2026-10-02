from django.contrib import admin

from .models import BreakoutPermission, BreakoutUsage, CallerIdMapping, OutboundRule, Trunk


class RuleInline(admin.TabularInline):
    model = OutboundRule
    extra = 0


@admin.register(Trunk)
class TrunkAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "name", "outbound_prefix", "sip_host", "port", "transport", "enabled")
    list_filter = ("event", "enabled", "transport")
    search_fields = ("name", "sip_host")
    inlines = [RuleInline]


@admin.register(OutboundRule)
class OutboundRuleAdmin(admin.ModelAdmin):
    list_display = ("id", "trunk", "name", "pattern", "allow", "per_call_max_minutes", "priority")
    list_filter = ("trunk", "allow")


@admin.register(CallerIdMapping)
class CallerIdMappingAdmin(admin.ModelAdmin):
    list_display = ("id", "trunk", "extension", "caller_id")
    raw_id_fields = ("extension",)


@admin.register(BreakoutPermission)
class BreakoutPermissionAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "extension", "user_group", "allowed", "daily_minutes_limit")
    list_filter = ("event", "allowed")
    raw_id_fields = ("extension", "user_group")


@admin.register(BreakoutUsage)
class BreakoutUsageAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "extension", "date", "calls", "minutes")
    list_filter = ("event", "date")
    raw_id_fields = ("extension",)
