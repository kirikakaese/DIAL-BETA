from django.contrib import admin

from .models import PhonebookSettings


@admin.register(PhonebookSettings)
class PhonebookSettingsAdmin(admin.ModelAdmin):
    list_display = ("event", "show_location", "show_owner", "directory_enabled", "updated_at")
    list_filter = ("directory_enabled",)
    search_fields = ("event__name", "event__slug")
    raw_id_fields = ("event",)
    # the token is a secret: visible for support, rotated only via the orga UI / API / CLI
    readonly_fields = ("directory_token",)
