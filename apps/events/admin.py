from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .models import Event, EventMembership, UserGroup, Webhook, validate_schedule


class EventAdminForm(forms.ModelForm):
    class Meta:
        model = Event
        fields = "__all__"

    def clean(self):
        data = super().clean()
        try:
            validate_schedule(data.get("state") or self.instance.state, data.get("registration_opens_at"),
                              data.get("goes_live_at"), data.get("archives_at"))
        except ValidationError as exc:
            self.add_error(None, exc)
        return data


class MembershipInline(admin.TabularInline):
    model = EventMembership
    extra = 0
    autocomplete_fields = ["user"]


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "state", "start_date", "end_date", "is_public", "location",
                    "registration_opens_at", "goes_live_at", "archives_at")
    list_filter = ("state", "is_public")
    search_fields = ("name", "slug", "location")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [MembershipInline]
    form = EventAdminForm
    fieldsets = (
        (None, {"fields": ("name", "slug", "description", "state", "is_public", "start_date", "end_date",
                           "location", "timezone", "default_language")}),
        (_("Schedule"), {"fields": ("registration_opens_at", "goes_live_at", "archives_at"),
                          "description": _("Lifecycle transitions applied automatically by the scheduler.")}),
        (_("Branding"), {"fields": ("logo", "primary_color", "accent_color", "announcement")}),
        (_("Dial plan"), {"fields": ("sip_domain", "dial_prefix", "has_gsm", "gsm_trunk")}),
        (_("Quotas & policies"), {"fields": ("max_extensions_per_user", "allow_guest_extensions", "allow_breakout",
                                              "cdr_aggregate_only", "cdr_retention_days")}),
        (_("Advanced"), {"classes": ("collapse",), "fields": ("cloned_from", "settings")}),
    )


@admin.register(UserGroup)
class UserGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "event")
    list_filter = ("event",)
    search_fields = ("name", "slug")


@admin.register(EventMembership)
class EventMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "event", "role")
    list_filter = ("event", "role")
    search_fields = ("user__username", "user__email")
    autocomplete_fields = ["user"]
    filter_horizontal = ["groups"]


@admin.register(Webhook)
class WebhookAdmin(admin.ModelAdmin):
    list_display = ("name", "event", "url", "is_active", "last_status", "last_delivery_at")
    list_filter = ("is_active", "event")
