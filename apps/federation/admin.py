from django.contrib import admin

from .models import FederationDirectoryEntry, FederationPeer


@admin.register(FederationPeer)
class FederationPeerAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "name", "remote_prefix", "sip_host", "sip_port", "transport", "srtp", "state",
                    "last_seen")
    list_filter = ("event", "state", "transport")
    search_fields = ("name", "sip_host", "remote_prefix", "remote_event_name")


@admin.register(FederationDirectoryEntry)
class FederationDirectoryEntryAdmin(admin.ModelAdmin):
    list_display = ("event_name", "instance_url", "dial_prefix", "sip_host", "fetched_at")
    search_fields = ("event_name", "instance_url")
