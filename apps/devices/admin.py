from django.contrib import admin

from .models import DECTManufacturer, Device, DeviceBinding, ProvisioningProfile


class BindingInline(admin.TabularInline):
    model = DeviceBinding
    extra = 0


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("__str__", "event", "type", "state", "owner", "ipei", "sip_username", "last_seen_rfp",
                    "last_seen_at")
    list_filter = ("event", "type", "state")
    search_fields = ("ipei", "sip_username", "name", "owner__username", "owner__email", "mac_address", "imsi",
                     "msisdn")
    autocomplete_fields = ["owner"]
    readonly_fields = ("sip_password", "subscription_pin", "provisioning_token", "gsm_register_token",
                       "gsm_registered_at", "manufacturer", "provisioning_url", "created_at", "updated_at")
    inlines = [BindingInline]
    fieldsets = (
        (None, {"fields": ("event", "owner", "type", "name", "state", "notes", "config")}),
        ("DECT", {"fields": ("ipei", "manufacturer", "handset_model", "uak", "subscription_pin",
                             "subscription_pin_expires_at", "omm_ppn", "omm_user_id", "last_seen_rfp",
                             "last_seen_at", "battery_percent", "rssi")}),
        ("SIP", {"fields": ("sip_username", "sip_password", "sip_password_rotated_at", "sip_transport",
                            "sip_user_agent", "sip_contact", "sip_registered_at")}),
        ("Autoprovisioning", {"fields": ("mac_address", "provisioning_profile", "provisioning_token",
                                         "provisioning_url")}),
        ("GSM", {"fields": ("imsi", "msisdn", ("gsm_2g", "gsm_3g", "gsm_4g", "gsm_5g"), "gsm_register_token",
                            "gsm_registered_at")}),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Manufacturer")
    def manufacturer(self, obj):
        m = obj.manufacturer
        return str(m) if m else "-"

    @admin.display(description="Provisioning URL")
    def provisioning_url(self, obj):
        return obj.provisioning_url or "-"


@admin.register(ProvisioningProfile)
class ProvisioningProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "vendor", "event", "content_type", "filename_pattern")
    list_filter = ("vendor", "event")


@admin.register(DECTManufacturer)
class DECTManufacturerAdmin(admin.ModelAdmin):
    list_display = ("emc", "name", "source", "models_hint", "created_at")
    list_filter = ("source",)
    search_fields = ("emc", "name", "models_hint")
    ordering = ("emc",)
