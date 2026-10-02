from django.contrib import admin

from .models import ConferenceParticipant, ConferenceRoom


@admin.register(ConferenceRoom)
class ConferenceRoomAdmin(admin.ModelAdmin):
    list_display = ("id", "extension", "name", "owner", "max_participants", "record", "is_public")
    search_fields = ("extension__number", "name", "owner__username")
    raw_id_fields = ("extension", "owner")


@admin.register(ConferenceParticipant)
class ConferenceParticipantAdmin(admin.ModelAdmin):
    list_display = ("id", "room", "caller_number", "channel_id", "joined_at", "left_at")
    raw_id_fields = ("room",)
