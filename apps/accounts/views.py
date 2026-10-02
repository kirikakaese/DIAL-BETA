"""Account views: registration, login, profile, service tokens, GDPR export/delete."""
import hashlib
import json
import logging
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.messages.views import SuccessMessageMixin
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from apps.core import antispam
from apps.core.audit import log as audit
from apps.core.middleware import client_ip
from apps.core.models import AuditLog
from apps.extensions import services as ext_services
from apps.extensions.models import Extension

from . import oidc
from . import tokens as token_mails
from .forms import (
    ChangeEmailForm,
    GdprDeleteForm,
    GuardedPasswordResetForm,
    LoginForm,
    ProfileForm,
    RegisterConfirmForm,
    RegisterEmailForm,
    RegisterForm,
    ServiceAccountForm,
)
from .models import RegistrationEmailToken, ServiceAccount, TokenError, User

seclog = logging.getLogger("pet.security")

# Failed logins are counted per client IP and per account over this window (seconds).
LOGIN_FAIL_WINDOW = 15 * 60


def _login_keys(request, email: str) -> tuple[str, str]:
    acct = hashlib.sha256(email.encode()).hexdigest()[:32] if email else ""
    return f"login:fail:ip:{client_ip(request)}", f"login:fail:acct:{acct}" if acct else ""


class LoginView(auth_views.LoginView):
    """Login with brute-force protection: after ``PET_LOGIN_MAX_FAILURES`` wrong passwords for one account
    (or ``PET_LOGIN_IP_MAX_FAILURES`` from one IP) within 15 minutes, further attempts are refused for
    ``PET_LOGIN_LOCKOUT_MINUTES`` - without revealing whether the account exists."""

    template_name = "accounts/login.html"
    form_class = LoginForm
    redirect_authenticated_user = True

    def _email(self) -> str:
        return (self.request.POST.get("username") or "").strip().lower()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(oidc.template_context())
        return ctx

    def post(self, request, *args, **kwargs):
        if not oidc.password_login_allowed():
            return self.render_to_response(self.get_context_data(form=self.form_class(request)), status=403)
        ip_key, acct_key = _login_keys(request, self._email())
        wait = max(antispam.locked_for(ip_key), antispam.locked_for(acct_key) if acct_key else 0)
        if wait:
            seclog.info("login refused (locked %ss) ip=%s", wait, client_ip(request))
            form = self.form_class(request)
            ctx = self.get_context_data(form=form, lockout_minutes=max(1, -(-wait // 60)))
            return self.render_to_response(ctx, status=429)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        email = self._email()
        ip_key, acct_key = _login_keys(self.request, email)
        lock_seconds = int(getattr(settings, "PET_LOGIN_LOCKOUT_MINUTES", 15)) * 60
        n_ip = antispam.hit(ip_key, LOGIN_FAIL_WINDOW)
        n_acct = antispam.hit(acct_key, LOGIN_FAIL_WINDOW) if acct_key else 0
        if acct_key and n_acct >= int(getattr(settings, "PET_LOGIN_MAX_FAILURES", 5)):
            antispam.lock(acct_key, lock_seconds)
            seclog.warning("login locked for account after %s failures ip=%s", n_acct, client_ip(self.request))
            user = User.objects.filter(email__iexact=email).first()
            if user is not None:
                audit(action="login", actor=None, target=user, request=self.request,
                      message=f"Login locked for {lock_seconds // 60} min after {n_acct} failed attempts")
        if n_ip >= int(getattr(settings, "PET_LOGIN_IP_MAX_FAILURES", 30)):
            antispam.lock(ip_key, lock_seconds)
            seclog.warning("login locked for ip=%s after %s failures", client_ip(self.request), n_ip)
        return super().form_invalid(form)

    def form_valid(self, form):
        _ip_key, acct_key = _login_keys(self.request, self._email())
        if acct_key:
            antispam.clear(acct_key)
        response = super().form_valid(form)
        if not form.cleaned_data.get("remember_me"):
            self.request.session.set_expiry(0)
        audit(action="login", actor=self.request.user, target=self.request.user, request=self.request,
              message="Web login")
        return response


@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("portal:dashboard")
    if not oidc.password_login_allowed():
        # SSO only: accounts are created on first OIDC login, the signup form is not offered.
        ctx = {"form": None, "email_only": False, **oidc.template_context()}
        return render(request, "accounts/register.html", ctx, status=403 if request.method == "POST" else 200)
    if settings.PET_REQUIRE_EMAIL_VERIFICATION:
        return _register_email_first(request)
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        audit(action="login", actor=user, target=user, request=request, message="Account created")
        messages.success(request, _("Welcome to PET, %(n)s! Pick an event to register your first extension.")
                         % {"n": user.username})
        if token_mails.token_mail_allowed(user.email, request) and token_mails.send_verification_mail(user, request):
            messages.info(request, _("We sent a verification link to %(e)s.") % {"e": user.email})
        return redirect("portal:dashboard")
    return render(request, "accounts/register.html", {"form": form, "email_only": False, **oidc.template_context()})


def _register_email_first(request):
    """Step 1 of e-mail-first signup: always answer with the same 'check your inbox' page."""
    form = RegisterEmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        if token_mails.token_mail_allowed(email, request):
            token_mails.send_registration_mail(email, request)
        return render(request, "accounts/register_sent.html", {"email": email})
    return render(request, "accounts/register.html", {"form": form, "email_only": True, **oidc.template_context()})


def _token_invalid(request, exc: TokenError, retry_url=None):
    return render(request, "accounts/token_invalid.html", {"reason": str(exc), "retry_url": retry_url}, status=400)


@require_http_methods(["GET", "POST"])
def register_confirm(request, token):
    """Step 2 of e-mail-first signup: nickname, password and language for the confirmed address."""
    if request.user.is_authenticated:
        return redirect("portal:dashboard")
    try:
        tok = RegistrationEmailToken.lookup(token, RegistrationEmailToken.Purpose.REGISTER)
    except TokenError as exc:
        return _token_invalid(request, exc, retry_url=reverse("accounts:register"))
    if User.objects.filter(email__iexact=tok.email).exists():
        return _token_invalid(request, TokenError("exists"), retry_url=reverse("accounts:login"))
    form = RegisterConfirmForm(request.POST or None, email=tok.email)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        tok.user = user
        tok.mark_used()
        tok.save(update_fields=["user"])
        login(request, user)
        audit(action="create", actor=user, target=user, request=request, message="Account created (e-mail confirmed)")
        messages.success(request, _("Welcome to PET, %(n)s! Pick an event to register your first extension.")
                         % {"n": user.username})
        return redirect("portal:dashboard")
    return render(request, "accounts/register_confirm.html", {"form": form, "email": tok.email})


@require_http_methods(["GET"])
def verify_email(request, token):
    try:
        tok = RegistrationEmailToken.lookup(token, RegistrationEmailToken.Purpose.VERIFY)
    except TokenError as exc:
        return _token_invalid(request, exc)
    user = tok.user
    if user is None or not user.is_active or user.email.lower() != tok.email:
        return _token_invalid(request, TokenError("invalid"))
    tok.mark_used()
    if not user.email_verified:
        user.email_verified = True
        user.save(update_fields=["email_verified"])
        audit(action="update", actor=user, target=user, request=request, message="E-mail address verified",
              changes={"email_verified": [False, True]})
    messages.success(request, _("Thanks, your e-mail address is verified."))
    return redirect("accounts:profile" if request.user.is_authenticated else "accounts:login")


@login_required
@require_http_methods(["POST"])
def resend_verification(request):
    user = request.user
    if user.email_verified:
        messages.info(request, _("Your e-mail address is already verified."))
    elif token_mails.token_mail_allowed(user.email, request) and token_mails.send_verification_mail(user, request):
        messages.success(request, _("We sent a new verification link to %(e)s.") % {"e": user.email})
    else:
        messages.warning(request, _("Could not send the mail right now. Please try again later."))
    return redirect("accounts:profile")


@login_required
@require_http_methods(["POST"])
def change_email(request):
    form = ChangeEmailForm(request.POST, user=request.user)
    if not form.is_valid():
        return _render_profile(request, change_form=form)
    new_email = form.cleaned_data["new_email"]
    if token_mails.token_mail_allowed(new_email, request) and token_mails.send_change_email_mail(
            request.user, new_email, request):
        messages.success(request, _("We sent a confirmation link to %(e)s. Your address changes once you open it.")
                         % {"e": new_email})
    else:
        messages.warning(request, _("Could not send the mail right now. Please try again later."))
    return redirect("accounts:profile")


@require_http_methods(["GET"])
def change_email_confirm(request, token):
    try:
        tok = RegistrationEmailToken.lookup(token, RegistrationEmailToken.Purpose.CHANGE_EMAIL)
    except TokenError as exc:
        return _token_invalid(request, exc)
    user = tok.user
    if user is None or not user.is_active or not tok.new_email:
        return _token_invalid(request, TokenError("invalid"))
    if User.objects.filter(email__iexact=tok.new_email).exclude(pk=user.pk).exists():
        return _token_invalid(request, TokenError("exists"))
    tok.mark_used()
    old_email = user.email
    user.email = tok.new_email
    user.email_verified = True
    user.save(update_fields=["email", "email_verified"])
    audit(action="update", actor=user, target=user, request=request, message="E-mail address changed",
          changes={"email": [old_email, user.email]})
    token_mails.send_change_email_notice(user, old_email, user.email)
    messages.success(request, _("Your e-mail address is now %(e)s.") % {"e": user.email})
    return redirect("accounts:profile" if request.user.is_authenticated else "accounts:login")


def _render_profile(request, form=None, change_form=None):
    memberships = request.user.memberships.select_related("event").prefetch_related("groups").order_by(
        "-event__start_date")
    return render(request, "accounts/profile.html", {
        "form": form or ProfileForm(instance=request.user),
        "change_form": change_form or ChangeEmailForm(user=request.user),
        "memberships": memberships,
        "token_count": request.user.service_accounts.filter(is_active=True).count(),
        "has_usable_password": request.user.has_usable_password(),
        **oidc.template_context(),
    })


@login_required
def profile(request):
    form = ProfileForm(request.POST or None, instance=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Profile saved."))
        return redirect("accounts:profile")
    return _render_profile(request, form=form)


@login_required
def tokens(request):
    raw_token = None
    form = ServiceAccountForm(user=request.user)
    if request.method == "POST":
        if request.POST.get("action") == "revoke":
            acct = request.user.service_accounts.filter(pk=request.POST.get("pk")).first()
            if acct:
                acct.is_active = False
                acct.save(update_fields=["is_active"])
                audit(action="delete", actor=request.user, target=acct, event=acct.event, request=request,
                      message="Service token revoked")
                messages.success(request, _("Token '%(n)s' revoked.") % {"n": acct.name})
            return redirect("accounts:tokens")
        form = ServiceAccountForm(request.POST, user=request.user)
        if form.is_valid():
            d = form.cleaned_data
            acct, raw_token = ServiceAccount.issue(name=d["name"], owner=request.user, event=d["event"],
                                                   scopes=d["scopes"], expires_at=d["expires_at"],
                                                   description=d["description"])
            audit(action="create", actor=request.user, target=acct, event=acct.event, request=request,
                  message="Service token created")
            messages.success(request, _("Token created. Copy it now - it will not be shown again."))
            form = ServiceAccountForm(user=request.user)
    accounts = request.user.service_accounts.select_related("event").order_by("-is_active", "name")
    return render(request, "accounts/tokens.html", {"form": form, "accounts": accounts, "raw_token": raw_token,
                                                    "now": timezone.now()})


def _gdpr_payload(user):
    exts = Extension.objects.filter(owner=user).select_related("event").prefetch_related("bindings__device")
    data = {
        "generated_at": timezone.now().isoformat(),
        "user": {
            "id": str(user.pk), "email": user.email, "username": user.username,
            "display_name": user.display_name,
            "date_joined": user.date_joined, "last_login": user.last_login, "email_verified": user.email_verified,
        },
        "memberships": [{"event": m.event.slug, "role": m.role, "groups": [g.slug for g in m.groups.all()],
                         "since": m.created_at} for m in user.memberships.select_related("event")],
        "extensions": [{
            "event": e.event.slug, "number": e.number, "type": e.type, "state": e.state,
            "display_name": e.display_name, "description": e.description, "location_hint": e.location_hint,
            "in_phonebook": e.in_phonebook, "created_at": e.created_at,
            "devices": [str(b.device_id) for b in e.bindings.all()],
        } for e in exts],
        "devices": [{
            "id": str(d.pk), "event": d.event.slug, "type": d.type, "name": d.name, "state": d.state,
            "ipei": d.ipei, "handset_model": d.handset_model, "sip_username": d.sip_username,
            "mac_address": d.mac_address, "last_seen_at": d.last_seen_at, "created_at": d.created_at,
        } for d in user.devices.select_related("event")],
        "audit": [{
            "at": a.created_at, "action": a.action, "event": a.event.slug if a.event else None,
            "target": a.target_repr, "message": a.message, "changes": a.changes,
        } for a in AuditLog.objects.filter(actor=user).select_related("event")[:5000]],
    }
    try:
        from apps.stats.services import gdpr_export as stats_export

        data["stats"] = stats_export(user)
    except ImportError:  # pragma: no cover - stats app optional
        pass
    return data


@login_required
def gdpr_export(request):
    body = json.dumps(_gdpr_payload(request.user), cls=DjangoJSONEncoder, indent=2, ensure_ascii=False)
    audit(action="other", actor=request.user, target=request.user, request=request, message="GDPR export")
    resp = HttpResponse(body, content_type="application/json")
    resp["Content-Disposition"] = f'attachment; filename="pet-export-{request.user.username}.json"'
    return resp


@login_required
@require_http_methods(["GET", "POST"])
def gdpr_delete(request):
    form = GdprDeleteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = request.user
        live = Extension.objects.filter(owner=user).exclude(
            state__in=[Extension.State.DELETED, Extension.State.EXPIRED, Extension.State.REJECTED])
        for ext in live:
            ext_services.delete(ext, user, request=request)
        try:
            from apps.stats.services import gdpr_delete as stats_delete

            stats_delete(user)
        except ImportError:  # pragma: no cover
            pass
        user.service_accounts.update(is_active=False)
        audit(action="delete", actor=user, target=user, request=request, message="GDPR account deletion")
        short = uuid.uuid4().hex[:12]
        user.email = f"deleted-{short}@invalid.local"
        user.username = f"deleted-{short}"
        user.display_name = ""
        user.is_active = False
        user.gdpr_erasure_requested_at = timezone.now()
        user.set_unusable_password()
        user.save()
        logout(request)
        messages.info(request, _("Your account has been deleted. Goodbye!"))
        return redirect("portal:home")
    ext_count = Extension.objects.filter(owner=request.user).exclude(state=Extension.State.DELETED).count()
    return render(request, "accounts/gdpr_delete.html", {"form": form, "ext_count": ext_count})


class PasswordChangeView(SuccessMessageMixin, auth_views.PasswordChangeView):
    template_name = "accounts/password_change.html"
    success_url = reverse_lazy("accounts:profile")
    success_message = _("Password changed.")


class PasswordResetView(auth_views.PasswordResetView):
    template_name = "accounts/password_reset.html"
    form_class = GuardedPasswordResetForm
    email_template_name = "registration/password_reset_email.html"
    subject_template_name = "registration/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password_reset_done")


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "accounts/password_reset_confirm.html"
    success_url = reverse_lazy("accounts:password_reset_complete")
