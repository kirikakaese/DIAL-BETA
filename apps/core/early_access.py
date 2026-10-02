"""Early-access gate: one shared password in front of the whole instance.

Active when ``DIAL_EARLY_ACCESS_PASSWORD`` is set (same contract as EVAC, ADR-0012 there). Browsers
without a valid gate cookie are sent to ``/early-access/`` (HTML) or get ``401`` (API). The cookie is
signed with ``SECRET_KEY`` and bound to a fingerprint of the current password, so changing the password
invalidates every cookie.

Not gated - machines that cannot type a password and authenticate with their own secret:

* phone provisioning ``/prov/...`` and remote phonebook XML ``/e/<slug>/phonebook/remote/<token>/...``
* PBX hooks, route lookups, dialplan, venue-agent snapshot/heartbeat (``X-DIAL-PBX-Secret`` header)
* API calls with a DIAL service token (``Authorization: Bearer dial_...``)
* the federation directory (it only lists events that opted into federation)
* static files and the PWA shell (manifest, service worker, offline page)

Each of those still checks its own secret in the view. The gate is a barrier in front of the normal
login, not an account system.
"""
from __future__ import annotations

import logging
import re

from django.conf import settings
from django.core import signing
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.deprecation import MiddlewareMixin
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

seclog = logging.getLogger("dial.security")

COOKIE = "dial_early_access"
SALT = "dial.early_access"
EXEMPT_PREFIXES = ("/early-access/", "/static/", "/prov/", "/manifest.webmanifest", "/sw.js", "/offline/",
                   "/favicon.ico", "/api/v1/federation/directory/")
REMOTE_PHONEBOOK = re.compile(r"^/e/[^/]+/phonebook/remote/")


def password() -> str:
    return getattr(settings, "DIAL_EARLY_ACCESS_PASSWORD", "") or ""


def enabled() -> bool:
    return bool(password())


def _fingerprint() -> str:
    return salted_hmac(SALT, password()).hexdigest()[:24]


def make_cookie_value() -> str:
    return signing.dumps({"fp": _fingerprint()}, salt=SALT, compress=True)


def cookie_valid(value: str | None) -> bool:
    if not value:
        return False
    max_age = int(getattr(settings, "DIAL_EARLY_ACCESS_DAYS", 30)) * 86400
    try:
        data = signing.loads(value, salt=SALT, max_age=max_age)
    except signing.BadSignature:
        return False
    return isinstance(data, dict) and constant_time_compare(str(data.get("fp", "")), _fingerprint())


def exempt(request) -> bool:
    path = request.path
    if path.startswith(EXEMPT_PREFIXES) or REMOTE_PHONEBOOK.match(path):
        return True
    if request.headers.get("X-DIAL-PBX-Secret"):
        return True
    auth = request.headers.get("Authorization", "")
    return auth.lower().startswith(("bearer ", "token ")) and auth.split(" ", 1)[-1].strip().startswith("dial_")


def granted(request) -> bool:
    return not enabled() or cookie_valid(request.COOKIES.get(COOKIE))


class EarlyAccessMiddleware(MiddlewareMixin):
    def process_request(self, request):
        if granted(request) or exempt(request):
            return None
        if request.path.startswith("/api/"):
            return JsonResponse({"detail": "This DIAL instance is in early access. Unlock it in a browser first, "
                                           "or use a service token."}, status=401)
        return redirect(f"{reverse('early_access')}?next={request.get_full_path()}")


def _safe_next(request, url: str) -> str:
    if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return url
    return "/"


@require_http_methods(["GET", "POST"])
def gate(request):
    next_url = _safe_next(request, request.POST.get("next") or request.GET.get("next") or "/")
    if not enabled() or granted(request):
        return redirect(next_url)
    error = False
    if request.method == "POST":
        if constant_time_compare(request.POST.get("password", ""), password()):
            resp = redirect(next_url)
            resp.set_cookie(COOKIE, make_cookie_value(), max_age=int(settings.DIAL_EARLY_ACCESS_DAYS) * 86400,
                            httponly=True, samesite="Lax",
                            secure=request.is_secure() or bool(getattr(settings, "SESSION_COOKIE_SECURE", False)))
            seclog.info("early access granted ip=%s", request.META.get("REMOTE_ADDR"))
            return resp
        error = True
        seclog.info("early access password wrong ip=%s", request.META.get("REMOTE_ADDR"))
    return render(request, "core/early_access.html", {
        "next": next_url, "error": error, "message": getattr(settings, "DIAL_EARLY_ACCESS_MESSAGE", ""),
    }, status=401 if error else 200)
