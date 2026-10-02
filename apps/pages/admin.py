from django.contrib import admin

from .models import InfoPage


@admin.register(InfoPage)
class InfoPageAdmin(admin.ModelAdmin):
    list_display = ("event", "title", "slug", "order", "published", "show_on_dashboard", "updated_at")
    list_filter = ("event", "published", "show_on_dashboard")
    search_fields = ("title", "slug", "body")
    raw_id_fields = ("event", "updated_by")
    prepopulated_fields = {"slug": ("title",)}
