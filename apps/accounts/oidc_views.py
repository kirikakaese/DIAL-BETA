"""OpenID Connect views: start the flow, handle the callback, link/unlink from the profile, IdP logout."""
from __future__ import annotations

import logging
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from apps.core import antispam
from apps.core.audit import log as audit
from apps.core.middleware import client_ip

from . import oidc
from .views import LOGIN_FAIL_WINDOW, _login_keys

seclog = logging.getLogger("pet.security")


def _require_enabled():
    if not oidc.enabled():
        raise Http404


def _error(request, message, status=400):
    return render(request, "accounts/oidc_error.html", {"message": message}, status=status)


def _count_failure(request):
    """Failed callbacks count like wrong passwords for the per-IP login lockout."""
    ip_key, _acct = _login_keys(request, "")
    n = antispam.hit(ip_key, LOGIN_FAIL_WINDOW)
    if n >= int(getattr(settings, "PET_LOGIN_IP_MAX_FAILURES", 30)):
        antispam.lock(ip_key, int(getattr(settings, "PET_LOGIN_LOCKOUT_MINUTES", 15)) * 60)
        seclog.warning("oidc callback locked for ip=%s after %s failures", client_ip(request), n)


@require_http_methods(["GET", "POST"])
def oidc_login(request):
    """Redirect to the identity provider. ``link=1`` (logged-in users) links the identity to the current account."""
    _require_enabled()
    data = request.POST if request.method == "POST" else request.GET
    link = bool(data.get("link")) and request.user.is_authenticated
    if request.user.is_authenticated and not link:
        return redirect(settings.LOGIN_REDIRECT_URL)
    try:
        url = oidc.start_flow(request, next_url=data.get("next", ""), link=link)
    except oidc.OIDCError as exc:
        return _error(request, str(exc), status=503)
    return redirect(url)


@require_http_methods(["GET"])
def oidc_callback(request):
    _require_enabled()
    ip_key, _acct = _login_keys(request, "")
    wait = antispam.locked_for(ip_key)
    if wait:
        seclog.info("oidc callback refused (locked %ss) ip=%s", wait, client_ip(request))
        return _error(request, _("Too many failed login attempts. Please wait a few minutes and try again."),
                      status=429)
    flow = oidc.pop_flow(request)
    state = request.GET.get("state", "")
    if not flow or not state or not secrets.compare_digest(state, flow["state"]):
        seclog.info("oidc callback with missing/mismatching state ip=%s", client_ip(request))
        return _error(request, _("The login session is invalid or has expired. Please start again."), status=400)
    if request.GET.get("error"):
        seclog.info("oidc provider returned error=%s", request.GET["error"])
        desc = request.GET.get("error_description") or request.GET["error"]
        return _error(request, _("The single sign-on provider refused the login: %(e)s") % {"e": desc}, status=400)
    code = request.GET.get("code", "")
    if not code:
        return _error(request, _("The login session is invalid or has expired. Please start again."), status=400)
    try:
        tokens = oidc.exchange_code(request, code, flow["verifier"])
        claims = oidc.claims_from_tokens(tokens, flow["nonce"])
    except oidc.OIDCError as exc:
        _count_failure(request)
        return _error(request, str(exc), status=400)

    if flow.get("link") and request.user.is_authenticated:
        try:
            oidc.link_user(request.user, claims, request=request)
        except oidc.OIDCError as exc:
            messages.error(request, str(exc))
            return redirect("accounts:profile")
        messages.success(request, _("Your single sign-on identity is now linked to this account."))
        return redirect("accounts:profile")

    try:
        user, how = oidc.resolve_user(claims, request=request)
    except oidc.OIDCError as exc:
        _count_failure(request)
        return _error(request, str(exc), status=403)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    if getattr(settings, "PET_OIDC_LOGOUT_AT_IDP", False) and tokens.get("id_token"):
        request.session[oidc.SESSION_ID_TOKEN_KEY] = tokens["id_token"]
    audit(action="login", actor=user, target=user, request=request, message="OIDC login")
    if how == "created":
        messages.success(request, _("Welcome to PET, %(n)s! Pick an event to register your first extension.")
                         % {"n": user.username})
    elif how == "linked":
        messages.info(request, _("Your single sign-on identity is now linked to your existing PET account."))
    target = flow.get("next") or settings.LOGIN_REDIRECT_URL
    return redirect(target)


@login_required
@require_http_methods(["POST"])
def oidc_unlink(request):
    _require_enabled()
    user = request.user
    if not user.oidc_subject:
        messages.info(request, _("No single sign-on identity is linked to this account."))
    elif not user.has_usable_password():
        messages.error(request, _("Set a password first - otherwise you could not log in anymore after unlinking."))
    else:
        old = user.oidc_subject
        user.oidc_subject = ""
        user.save(update_fields=["oidc_subject"])
        audit(action="update", actor=user, target=user, request=request, message="OIDC identity unlinked",
              changes={"oidc_subject": [old, ""]})
        messages.success(request, _("The single sign-on identity was unlinked from your account."))
    return redirect("accounts:profile")


class LogoutView(auth_views.LogoutView):
    """Standard logout; with ``PET_OIDC_LOGOUT_AT_IDP`` the browser is sent on to the IdP's end-session endpoint."""

    def post(self, request, *args, **kwargs):
        id_token = request.session.get(oidc.SESSION_ID_TOKEN_KEY)
        response = super().post(request, *args, **kwargs)
        if id_token:
            after = request.build_absolute_uri(getattr(response, "url", None) or "/")
            url = oidc.end_session_url(id_token, after)
            if url:
                return redirect(url)
        return response
