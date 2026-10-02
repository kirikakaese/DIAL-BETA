from django.contrib import admin

from .models import Broadcast, Message


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "direction", "sender", "recipient_extension", "state", "sent_at", "created_at")
    list_filter = ("event", "direction", "state")
    search_fields = ("text", "recipient_extension__number", "sender__username")
    raw_id_fields = ("sender", "sender_extension", "recipient_extension", "recipient_group", "broadcast")


@admin.register(Broadcast)
class BroadcastAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "target", "group", "sender", "sent_count", "failed_count", "created_at")
    list_filter = ("event", "target")
    raw_id_fields = ("sender", "group")
