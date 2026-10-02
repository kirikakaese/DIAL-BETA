"""Admin for the Asterisk realtime tables (read-mostly), the sync log and the PBX outbox."""
from django.contrib import admin
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import (
    Cdr,
    DialplanEntry,
    PBXConnection,
    PBXJob,
    PBXSyncLog,
    PsAor,
    PsAuth,
    PsContact,
    PsEndpoint,
    PsEndpointIdIp,
    VoicemailUser,
)


class RealtimeAdmin(admin.ModelAdmin):
    """Rows are owned by the sync code; allow inspection and emergency edits, no bulk delete."""

    actions = None
    save_on_top = True


@admin.register(PsEndpoint)
class PsEndpointAdmin(RealtimeAdmin):
    list_display = ("id", "context", "callerid", "transport", "allow", "mailboxes", "accountcode")
    list_filter = ("context", "transport", "accountcode")
    search_fields = ("id", "callerid", "context")


@admin.register(PsAuth)
class PsAuthAdmin(RealtimeAdmin):
    list_display = ("id", "auth_type", "username")
    search_fields = ("id", "username")
    exclude = ("password", "md5_cred")


@admin.register(PsAor)
class PsAorAdmin(RealtimeAdmin):
    list_display = ("id", "max_contacts", "qualify_frequency", "mailboxes")
    search_fields = ("id",)


@admin.register(PsContact)
class PsContactAdmin(RealtimeAdmin):
    list_display = ("endpoint", "uri", "user_agent", "expiration_time", "via_addr", "via_port")
    search_fields = ("id", "endpoint", "uri", "user_agent")
    readonly_fields = [f.name for f in PsContact._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(PsEndpointIdIp)
class PsEndpointIdIpAdmin(RealtimeAdmin):
    list_display = ("id", "endpoint", "match")


@admin.register(DialplanEntry)
class DialplanEntryAdmin(RealtimeAdmin):
    list_display = ("context", "exten", "priority", "app", "appdata")
    list_filter = ("context",)
    search_fields = ("exten", "appdata")
    ordering = ("context", "exten", "priority")
    list_per_page = 200


@admin.register(VoicemailUser)
class VoicemailUserAdmin(RealtimeAdmin):
    list_display = ("mailbox", "context", "fullname", "email", "maxmsg")
    list_filter = ("context",)
    search_fields = ("mailbox", "fullname")
    exclude = ("password",)


@admin.register(Cdr)
class CdrAdmin(admin.ModelAdmin):
    list_display = ("start", "src", "dst", "disposition", "duration", "billsec", "accountcode", "ingested_at")
    list_filter = ("disposition", "accountcode")
    search_fields = ("src", "dst", "uniqueid", "channel")
    date_hierarchy = "start"
    readonly_fields = [f.name for f in Cdr._meta.fields if f.name != "ingested_at"]

    def has_add_permission(self, request):
        return False


@admin.register(PBXConnection)
class PBXConnectionAdmin(admin.ModelAdmin):
    """Per-event venue PBX. Orga normally edit this on /e/<slug>/pbx/; the admin is for support."""

    list_display = ("event", "backend", "provisioning", "ari_url", "ami_host", "has_hook_secret", "agent_last_seen",
                    "updated_at")
    list_filter = ("backend", "provisioning")
    search_fields = ("event__slug", "event__name", "ari_url", "ami_host", "notes", "agent_host")
    readonly_fields = ("created_at", "updated_at", "agent_last_seen", "agent_version", "agent_host",
                       "agent_software", "agent_asterisk_ok", "agent_message")
    autocomplete_fields = ("event",)
    fieldsets = (
        (None, {"fields": ("event", "backend", "provisioning", "notes")}),
        (_("ARI"), {"fields": ("ari_url", "ari_user", "ari_password", "ari_app")}),
        (_("AMI"), {"fields": ("ami_host", "ami_port", "ami_user", "ami_password")}),
        (_("Hooks"), {"fields": ("hook_secret",)}),
        (_("Venue agent"), {"fields": ("agent_poll_interval", "agent_last_seen", "agent_version", "agent_host",
                                       "agent_software", "agent_asterisk_ok", "agent_message")}),
        (_("Timestamps"), {"fields": ("created_at", "updated_at")}),
    )

    @admin.display(boolean=True, description=_("hook secret"))
    def has_hook_secret(self, obj):
        return bool(obj.hook_secret)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from apps.pbx import reset_pbx_cache

        reset_pbx_cache()

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        from apps.pbx import reset_pbx_cache

        reset_pbx_cache()


@admin.register(PBXSyncLog)
class PBXSyncLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "event", "kind", "target", "ok", "rows", "message")
    list_filter = ("kind", "ok", "event")
    search_fields = ("target", "message")
    readonly_fields = [f.name for f in PBXSyncLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PBXJob)
class PBXJobAdmin(admin.ModelAdmin):
    list_display = ("created_at", "event", "kind", "target_type", "target_id", "state", "attempts", "priority",
                    "next_attempt_at", "delivered_at", "short_error")
    list_filter = ("state", "kind", "event", "target_type")
    search_fields = ("target_id", "dedupe_key", "last_error")
    readonly_fields = ("id", "created_at", "sent_at", "delivered_at", "attempts", "result", "last_error")
    date_hierarchy = "created_at"
    actions = ["retry_jobs", "deliver_now"]

    @admin.display(description=_("error"))
    def short_error(self, obj):
        return (obj.last_error or "")[:80]

    @admin.action(description=_("Retry selected jobs (reset to pending)"))
    def retry_jobs(self, request, queryset):
        n = queryset.exclude(state=PBXJob.State.DELIVERED).update(
            state=PBXJob.State.PENDING, attempts=0, next_attempt_at=timezone.now(), last_error="")
        self.message_user(request, _("%(n)d job(s) reset to pending.") % {"n": n})

    @admin.action(description=_("Deliver selected jobs now"))
    def deliver_now(self, request, queryset):
        from . import outbox

        ok = 0
        for job in queryset.exclude(state=PBXJob.State.DELIVERED):
            ok += outbox.deliver(job)
        self.message_user(request, _("%(n)d job(s) delivered.") % {"n": ok})
