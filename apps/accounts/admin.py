"""Admin for users (e-mail login) and service accounts."""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.utils.translation import gettext_lazy as _

from .models import RegistrationEmailToken, ServiceAccount, User


class AdminUserCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("email", "username")


class AdminUserChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User


class HasSSOFilter(admin.SimpleListFilter):
    title = _("SSO identity")
    parameter_name = "sso"

    def lookups(self, request, model_admin):
        return (("yes", _("linked")), ("no", _("not linked")))

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.exclude(oidc_subject="")
        if self.value() == "no":
            return queryset.filter(oidc_subject="")
        return queryset


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    form = AdminUserChangeForm
    add_form = AdminUserCreationForm
    ordering = ["email"]
    list_display = ["email", "username", "display_name", "is_active", "is_staff", "email_verified", "date_joined"]
    list_filter = ["is_active", "is_staff", "is_superuser", "email_verified", HasSSOFilter]
    search_fields = ["email", "username", "display_name", "oidc_subject"]
    readonly_fields = ["date_joined", "last_login", "gdpr_erasure_requested_at"]
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("Profile"), {"fields": ("username", "display_name", "email_verified")}),
        (_("External identity"), {"fields": ("oidc_subject", "ldap_dn"), "classes": ("collapse",)}),
        (_("Permissions"), {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        (_("Important dates"), {"fields": ("last_login", "date_joined", "gdpr_erasure_requested_at")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "username", "password1", "password2")}),)


@admin.register(ServiceAccount)
class ServiceAccountAdmin(admin.ModelAdmin):
    list_display = ["name", "token_prefix", "owner", "event", "is_active", "last_used_at", "expires_at"]
    list_filter = ["is_active", "event"]
    search_fields = ["name", "token_prefix", "owner__email", "owner__username"]
    readonly_fields = ["token_prefix", "token_hash", "created_at", "last_used_at"]
    autocomplete_fields = ["owner"]


@admin.register(RegistrationEmailToken)
class RegistrationEmailTokenAdmin(admin.ModelAdmin):
    """Read-only: tokens are issued by the signup/profile flows; only the hash is stored."""

    list_display = ["email", "purpose", "user", "new_email", "created_at", "expires_at", "used_at", "ip"]
    list_filter = ["purpose", "created_at"]
    search_fields = ["email", "new_email", "user__email", "user__username"]
    readonly_fields = ["email", "purpose", "user", "new_email", "created_at", "expires_at", "used_at", "ip"]
    exclude = ["token_hash"]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
