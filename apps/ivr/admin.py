from django.contrib import admin

from .models import Announcement, FunService, IVRMenu


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ("id", "extension", "language", "loop", "audio", "updated_at")
    search_fields = ("extension__number", "tts_text")
    raw_id_fields = ("extension",)


@admin.register(IVRMenu)
class IVRMenuAdmin(admin.ModelAdmin):
    list_display = ("id", "extension", "language", "timeout", "invalid_retries", "updated_at")
    search_fields = ("extension__number", "prompt_tts")
    raw_id_fields = ("extension",)


@admin.register(FunService)
class FunServiceAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "kind", "extension")
    list_filter = ("event", "kind")
    raw_id_fields = ("extension",)
