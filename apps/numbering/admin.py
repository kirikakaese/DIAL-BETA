from django.contrib import admin

from .models import ExtensionClaim, ExtensionPool, NumberPlan, NumberRange


class NumberRangeInline(admin.TabularInline):
    model = NumberRange
    extra = 0
    fields = ("priority", "name", "prefix", "pattern", "min_length", "max_length", "mode", "is_vanity",
              "allowed_roles", "allowed_types", "quota_per_user", "is_active")


@admin.register(NumberPlan)
class NumberPlanAdmin(admin.ModelAdmin):
    list_display = ("event", "min_length", "max_length", "prefix_free", "default_requires_approval",
                    "default_allowed")
    list_filter = ("prefix_free",)
    inlines = [NumberRangeInline]


@admin.register(NumberRange)
class NumberRangeAdmin(admin.ModelAdmin):
    list_display = ("name", "plan", "priority", "prefix", "pattern", "mode", "is_vanity", "is_active")
    list_filter = ("mode", "is_vanity", "is_active", "plan__event")
    filter_horizontal = ["allowed_groups"]


@admin.register(ExtensionPool)
class ExtensionPoolAdmin(admin.ModelAdmin):
    list_display = ("name", "event", "prefix", "length", "is_active")
    list_filter = ("is_active", "event")


@admin.register(ExtensionClaim)
class ExtensionClaimAdmin(admin.ModelAdmin):
    list_display = ("number", "event", "type", "user", "email", "valid_until", "redeemed_at", "created_by")
    list_filter = ("event", "type")
    search_fields = ("number", "email", "user__username", "user__email")
    readonly_fields = ("token", "redeemed_at", "redeemed_extension", "created_by")
    raw_id_fields = ("user",)
