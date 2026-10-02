from django.contrib import admin

from apps.devices.models import DeviceBinding

from .models import Extension, ExtensionRequest, ExtensionTransfer


class BindingInline(admin.TabularInline):
    model = DeviceBinding
    extra = 0
    autocomplete_fields = ["device"]


@admin.register(Extension)
class ExtensionAdmin(admin.ModelAdmin):
    list_display = ("number", "event", "type", "state", "owner", "display_name", "in_phonebook",
                    "provisioned_at")
    list_filter = ("event", "type", "state", "in_phonebook", "is_temporary")
    search_fields = ("number", "display_name", "owner__username", "owner__email", "description")
    autocomplete_fields = ["owner", "moderated_by", "ported_from"]
    readonly_fields = ("provisioned_at", "provision_error", "claim_token", "created_at", "updated_at")
    inlines = [BindingInline]


@admin.register(ExtensionRequest)
class ExtensionRequestAdmin(admin.ModelAdmin):
    list_display = ("number", "event", "user", "type", "created_at", "notified_at")
    list_filter = ("event",)


@admin.register(ExtensionTransfer)
class ExtensionTransferAdmin(admin.ModelAdmin):
    list_display = ("extension", "from_user", "to_user", "accepted_at", "expires_at")
