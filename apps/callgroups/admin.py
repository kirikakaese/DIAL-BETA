from django.contrib import admin

from .models import CallGroup, CallGroupInvite, GroupLoginLog, GroupMember


class GroupMemberInline(admin.TabularInline):
    model = GroupMember
    extra = 0
    raw_id_fields = ("extension", "added_by")


@admin.register(CallGroup)
class CallGroupAdmin(admin.ModelAdmin):
    list_display = ("number", "name", "event", "strategy", "shortcode", "ring_timeout", "allow_self_service")
    list_filter = ("event", "strategy")
    search_fields = ("extension__number", "extension__display_name", "shortcode")
    raw_id_fields = ("event", "extension", "user_group", "admins")
    inlines = [GroupMemberInline]


@admin.register(CallGroupInvite)
class CallGroupInviteAdmin(admin.ModelAdmin):
    list_display = ("created_at", "group", "extension", "invited_by", "status", "responded_at")
    list_filter = ("accepted",)
    search_fields = ("extension__number", "group__extension__number")
    raw_id_fields = ("group", "extension", "invited_by")
    readonly_fields = ("token", "created_at")


@admin.register(GroupLoginLog)
class GroupLoginLogAdmin(admin.ModelAdmin):
    list_display = ("at", "member", "action", "via")
    list_filter = ("action", "via")
    raw_id_fields = ("member",)
