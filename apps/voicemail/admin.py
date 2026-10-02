from django.contrib import admin

from .models import Mailbox, Message, MessageDelivery


@admin.register(Mailbox)
class MailboxAdmin(admin.ModelAdmin):
    list_display = ("number", "event", "enabled", "email_delivery", "max_messages", "updated_at")
    list_filter = ("event", "enabled", "email_delivery")
    search_fields = ("extension__number", "extension__owner__username")
    raw_id_fields = ("event", "extension")
    exclude = ("pin",)  # never show PINs in the admin list/forms


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    """Metadata only - audio is not exposed here (privacy)."""

    list_display = ("received_at", "mailbox", "caller_number", "duration_seconds", "is_read")
    list_filter = ("is_read", "mailbox__event")
    search_fields = ("mailbox__extension__number", "caller_number")
    raw_id_fields = ("mailbox",)
    readonly_fields = ("audio", "asterisk_msg_id")


@admin.register(MessageDelivery)
class MessageDeliveryAdmin(admin.ModelAdmin):
    list_display = ("sent_at", "message", "kind", "recipient", "ok")
    list_filter = ("kind", "ok")
    raw_id_fields = ("message",)
