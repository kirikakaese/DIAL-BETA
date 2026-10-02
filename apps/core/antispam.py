"""Cheap anti-abuse helpers shared by the account forms: cache counters/lockouts and a form spam guard.

Nothing here needs a third-party service. It is meant to stop the boring 95 %: credential stuffing at
a few requests per second, and bots that fill every field of a signup form within a second.
"""
from __future__ import annotations

import logging
import time

from django import forms
from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.utils.translation import gettext_lazy as _

log = logging.getLogger("dial.security")


# --------------------------------------------------------------------------- counters / lockouts

def hit(key: str, window: int) -> int:
    """Increment a fixed-window counter and return the new value (1 if the cache is unavailable)."""
    try:
        if cache.add(key, 1, window):
            return 1
        return cache.incr(key)
    except Exception:  # noqa: BLE001 - cache down: fail open, but never crash a login
        return 1


def lock(key: str, seconds: int) -> None:
    try:
        cache.set(f"{key}:lock", int(time.time()) + seconds, seconds)
    except Exception:  # noqa: BLE001
        pass


def locked_for(key: str) -> int:
    """Seconds the key stays locked (0 = not locked)."""
    try:
        until = cache.get(f"{key}:lock")
    except Exception:  # noqa: BLE001
        return 0
    return max(0, int(until) - int(time.time())) if until else 0


def clear(key: str) -> None:
    try:
        cache.delete_many([key, f"{key}:lock"])
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- form spam guard

def spam_guard_enabled() -> bool:
    return bool(getattr(settings, "DIAL_SPAM_GUARD", True))


_signer = signing.TimestampSigner(salt="dial.spamguard")


class SpamGuardMixin:
    """Honeypot + time trap for public forms.

    Adds two extra fields: ``website`` (the honeypot - hidden via ``.hp`` in ``_form.html``, must stay empty)
    and ``form_ts`` (signed issue time). Submissions that fill the honeypot, arrive faster than
    ``DIAL_SPAM_GUARD_MIN_SECONDS`` or carry a missing/forged/too old timestamp are rejected with one
    generic error. ``check_timing = False`` keeps only the honeypot (login: password managers submit fast).
    """

    check_timing = True
    max_age = 6 * 3600

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not spam_guard_enabled():
            return
        self.fields["website"] = forms.CharField(
            required=False, label=_("Website"), max_length=200,
            widget=forms.TextInput(attrs={"class": "hp", "autocomplete": "off", "tabindex": "-1",
                                          "aria-hidden": "true"}),
        )
        if self.check_timing:
            self.fields["form_ts"] = forms.CharField(required=False, widget=forms.HiddenInput)
            if not self.is_bound:
                self.fields["form_ts"].initial = _signer.sign("t")

    def clean(self):
        cleaned = super().clean()
        if not spam_guard_enabled():
            return cleaned
        if cleaned.get("website"):
            log.info("spam guard: honeypot filled on %s", type(self).__name__)
            raise forms.ValidationError(_("Spam protection triggered. Please try again."), code="spam")
        if self.check_timing:
            min_seconds = getattr(settings, "DIAL_SPAM_GUARD_MIN_SECONDS", 3)
            ts = cleaned.get("form_ts") or ""
            try:
                _signer.unsign(ts, max_age=self.max_age)
            except signing.BadSignature:
                log.info("spam guard: bad timestamp on %s", type(self).__name__)
                raise forms.ValidationError(_("Spam protection triggered. Please try again."), code="spam")
            age = _age_seconds(ts)
            if age < min_seconds:
                log.info("spam guard: form submitted after %ss on %s", age, type(self).__name__)
                raise forms.ValidationError(_("That was quick - please wait a moment and submit again."),
                                            code="too_fast")
        return cleaned


def _age_seconds(signed: str) -> int:
    """Seconds since the TimestampSigner value was issued (``value:timestamp:sig``, base62 timestamp)."""
    try:
        ts_b62 = signed.rsplit(_signer.sep, 2)[1]
        return int(time.time()) - signing.b62_decode(ts_b62)
    except (IndexError, ValueError):
        return 0
