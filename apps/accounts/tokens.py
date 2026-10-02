"""E-mail confirmation tokens: mail delivery helpers and a small abuse limiter.

All mails are plain text, rendered from ``templates/registration/*.txt`` and link back to
``PET_PUBLIC_URL``. Nothing here reveals whether an address already has an account.
"""
from __future__ import annotations

import hashlib
import logging

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.core.middleware import client_ip

from .models import RegistrationEmailToken, User

log = logging.getLogger("pet.accounts.tokens")

# Token mails per hour, per e-mail address and per client IP.
TOKEN_MAILS_PER_EMAIL = 5
TOKEN_MAILS_PER_IP = 20
RATE_WINDOW = 3600

SUBJECT_PREFIX = "[PET] "


# --- rate limiting ----------------------------------------------------------
def _bump(key: str, limit: int) -> bool:
    """Fixed-window counter; returns ``True`` while under ``limit``. Fails open if the cache is down."""
    try:
        if cache.add(key, 1, RATE_WINDOW):
            return True
        return cache.incr(key) <= limit
    except Exception:  # cache unavailable / no incr support
        return True


def token_mail_allowed(email: str, request=None) -> bool:
    """Count one token mail for ``email`` (and the request IP) and tell whether it may be sent."""
    email_key = hashlib.sha256(RegistrationEmailToken.normalize(email).encode()).hexdigest()[:32]
    ok = _bump(f"rl:tokenmail:email:{email_key}", TOKEN_MAILS_PER_EMAIL)
    if request is not None:
        ok = _bump(f"rl:tokenmail:ip:{client_ip(request)}", TOKEN_MAILS_PER_IP) and ok
    return ok


# --- mail helpers -----------------------------------------------------------
def public_url(url_name: str, raw_token: str) -> str:
    return settings.PET_PUBLIC_URL.rstrip("/") + reverse(url_name, args=[raw_token])


def _send(template: str, subject: str, to: str, context: dict) -> bool:
    ctx = {"public_url": settings.PET_PUBLIC_URL.rstrip("/"),
           "ttl_hours": getattr(settings, "PET_EMAIL_TOKEN_TTL_HOURS", 48), **context}
    body = render_to_string(f"registration/{template}", ctx)
    full_subject = SUBJECT_PREFIX + str(subject)
    try:
        send_mail(full_subject, body, settings.DEFAULT_FROM_EMAIL, [to])
        return True
    except Exception:  # SMTP down must not break signup; the user can request a new link
        log.exception("Could not send %s to %s", template, to)
        return False


def _ip(request):
    return client_ip(request) if request is not None else None


def send_registration_mail(email: str, request=None) -> bool:
    """Email-first signup: send either a registration link or an 'account exists' notice."""
    email = RegistrationEmailToken.normalize(email)
    if User.objects.filter(email__iexact=email).exists():
        return _send("account_exists_email.txt", _("You already have an account"), email, {
            "email": email,
            "reset_url": settings.PET_PUBLIC_URL.rstrip("/") + reverse("accounts:password_reset"),
            "login_url": settings.PET_PUBLIC_URL.rstrip("/") + reverse("accounts:login"),
        })
    _tok, raw = RegistrationEmailToken.issue(email, RegistrationEmailToken.Purpose.REGISTER, ip=_ip(request))
    return _send("register_confirm_email.txt", _("Confirm your e-mail address"), email, {
        "email": email, "confirm_url": public_url("accounts:register_confirm", raw),
    })


def send_verification_mail(user, request=None) -> bool:
    _tok, raw = RegistrationEmailToken.issue(user.email, RegistrationEmailToken.Purpose.VERIFY, user=user,
                                          ip=_ip(request))
    return _send("verify_email.txt", _("Verify your e-mail address"), user.email, {
        "user": user, "verify_url": public_url("accounts:verify_email", raw),
    })


def send_change_email_mail(user, new_email: str, request=None) -> bool:
    new_email = RegistrationEmailToken.normalize(new_email)
    _tok, raw = RegistrationEmailToken.issue(new_email, RegistrationEmailToken.Purpose.CHANGE_EMAIL, user=user,
                                          new_email=new_email, ip=_ip(request))
    return _send("change_email.txt", _("Confirm your new e-mail address"), new_email, {
        "user": user, "new_email": new_email, "confirm_url": public_url("accounts:change_email_confirm", raw),
    })


def send_change_email_notice(user, old_email: str, new_email: str) -> bool:
    return _send("change_email_notice.txt", _("Your e-mail address was changed"), old_email, {
        "user": user, "old_email": old_email, "new_email": new_email,
    })


def send_invitation_mail(user, event=None, request=None) -> bool:
    """Invite an account created on the user's behalf (CSV import): a set-your-password link.

    Uses Django's password-reset token, which also works for accounts without a usable password.
    """
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    url = settings.PET_PUBLIC_URL.rstrip("/") + reverse("accounts:password_reset_confirm", args=[uid, token])
    return _send("invitation_email.txt", _("Your PET account"), user.email, {
        "user": user, "event": event, "set_password_url": url,
        "login_url": settings.PET_PUBLIC_URL.rstrip("/") + reverse("accounts:login"),
    })
