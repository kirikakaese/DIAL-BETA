"""Signup/login abuse protection: honeypot + time trap, login lockout, domain lists, verification gate."""
import hashlib
from unittest import mock

import pytest
from django import forms
from django.core import signing
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from apps.accounts.forms import RegisterEmailForm, RegisterForm, validate_signup_email
from apps.accounts.models import User
from apps.core import antispam
from apps.core.models import AuditLog
from apps.extensions import services
from apps.extensions.models import ExtensionType

pytestmark = pytest.mark.django_db

PW = "a-very-strong-pw-42"
SIGNUP = {"email": "new@example.org", "username": "newbie", "password1": PW, "password2": PW}


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


def _fresh_ts(age_seconds=0):
    """A validly signed ``form_ts`` issued ``age_seconds`` ago."""
    signer = signing.TimestampSigner(salt="dial.spamguard")
    with mock.patch("django.core.signing.time.time", return_value=signing.time.time() - age_seconds):
        return signer.sign("t")


# --- spam guard on the signup form -------------------------------------------------------------
@override_settings(DIAL_SPAM_GUARD=True, DIAL_SPAM_GUARD_MIN_SECONDS=3, DIAL_REQUIRE_EMAIL_VERIFICATION=False)
def test_register_form_renders_honeypot_and_timestamp(client):
    r = client.get(reverse("accounts:register"))
    assert r.status_code == 200
    assert b'name="website"' in r.content and b'class="hp"' in r.content
    assert b'name="form_ts"' in r.content
    assert b'aria-hidden="true"' in r.content


@override_settings(DIAL_SPAM_GUARD=True, DIAL_SPAM_GUARD_MIN_SECONDS=3, DIAL_REQUIRE_EMAIL_VERIFICATION=False)
def test_register_honeypot_filled_rejected(client):
    r = client.post(reverse("accounts:register"), {**SIGNUP, "website": "http://spam.example",
                                                    "form_ts": _fresh_ts(10)})
    assert r.status_code == 200 and b"Spam protection triggered" in r.content
    assert not User.objects.filter(email="new@example.org").exists()


@override_settings(DIAL_SPAM_GUARD=True, DIAL_SPAM_GUARD_MIN_SECONDS=3, DIAL_REQUIRE_EMAIL_VERIFICATION=False)
def test_register_missing_or_forged_timestamp_rejected(client):
    r = client.post(reverse("accounts:register"), SIGNUP)
    assert r.status_code == 200 and b"Spam protection triggered" in r.content
    r = client.post(reverse("accounts:register"), {**SIGNUP, "form_ts": "t:abc:forged"})
    assert r.status_code == 200 and b"Spam protection triggered" in r.content
    assert not User.objects.filter(email="new@example.org").exists()


@override_settings(DIAL_SPAM_GUARD=True, DIAL_SPAM_GUARD_MIN_SECONDS=3, DIAL_REQUIRE_EMAIL_VERIFICATION=False)
def test_register_too_fast_rejected(client):
    r = client.post(reverse("accounts:register"), {**SIGNUP, "form_ts": _fresh_ts(0)})
    assert r.status_code == 200 and b"That was quick" in r.content
    assert not User.objects.filter(email="new@example.org").exists()


@override_settings(DIAL_SPAM_GUARD=True, DIAL_SPAM_GUARD_MIN_SECONDS=3, DIAL_REQUIRE_EMAIL_VERIFICATION=False)
def test_register_human_pace_accepted(client):
    r = client.post(reverse("accounts:register"), {**SIGNUP, "form_ts": _fresh_ts(10), "website": ""})
    assert r.status_code == 302
    assert User.objects.filter(email="new@example.org").exists()


@override_settings(DIAL_SPAM_GUARD=True, DIAL_SPAM_GUARD_MIN_SECONDS=3, DIAL_REQUIRE_EMAIL_VERIFICATION=True)
def test_email_first_signup_guarded_too(client):
    r = client.post(reverse("accounts:register"), {"email": "x@example.org", "website": "bot"})
    assert r.status_code == 200 and b"Spam protection triggered" in r.content
    r = client.post(reverse("accounts:register"), {"email": "x@example.org", "form_ts": _fresh_ts(10)})
    assert r.status_code == 200 and b"Check your inbox" in r.content


@override_settings(DIAL_SPAM_GUARD=False)
def test_guard_disabled_adds_no_fields():
    assert "website" not in RegisterForm().fields and "form_ts" not in RegisterEmailForm().fields


# --- login: honeypot only + lockout ------------------------------------------------------------
@override_settings(DIAL_SPAM_GUARD=True)
def test_login_has_honeypot_but_no_time_trap(client, user):
    r = client.get(reverse("accounts:login"))
    assert b'name="website"' in r.content and b'name="form_ts"' not in r.content
    r = client.post(reverse("accounts:login"), {"username": user.email, "password": "pw-alice-1234"})
    assert r.status_code == 302
    client.logout()
    r = client.post(reverse("accounts:login"), {"username": user.email, "password": "pw-alice-1234",
                                                "website": "bot"})
    assert r.status_code == 200 and b"Spam protection triggered" in r.content


@override_settings(DIAL_LOGIN_MAX_FAILURES=5, DIAL_LOGIN_LOCKOUT_MINUTES=15)
def test_login_lockout_after_repeated_failures(client, user):
    url = reverse("accounts:login")
    for _ in range(4):
        r = client.post(url, {"username": user.email, "password": "nope"})
        assert r.status_code == 200
    r = client.post(url, {"username": user.email, "password": "nope"})
    assert r.status_code == 200  # 5th failure triggers the lock
    assert AuditLog.objects.filter(action="login", target_id=str(user.pk), actor=None).exists()
    # locked: even the right password is refused, without touching the counter
    r = client.post(url, {"username": user.email, "password": "pw-alice-1234"})
    assert r.status_code == 429 and b"Too many failed login attempts" in r.content
    assert b"15" in r.content
    assert "_auth_user_id" not in client.session
    # other accounts are unaffected
    User.objects.create_user(email="carol@example.org", username="carol", password="pw-carol-12345")
    r = client.post(url, {"username": "carol@example.org", "password": "pw-carol-12345"})
    assert r.status_code == 302
    client.logout()
    # lock expires (simulated) -> login works and clears the failure counter
    cache.clear()
    r = client.post(url, {"username": user.email, "password": "pw-alice-1234"})
    assert r.status_code == 302
    assert cache.get(f"login:fail:acct:{hashlib.sha256(user.email.encode()).hexdigest()[:32]}") is None


@override_settings(DIAL_LOGIN_MAX_FAILURES=5)
def test_login_lockout_does_not_reveal_unknown_accounts(client):
    url = reverse("accounts:login")
    for _ in range(5):
        client.post(url, {"username": "ghost@example.org", "password": "nope"})
    r = client.post(url, {"username": "ghost@example.org", "password": "nope"})
    assert r.status_code == 429


@override_settings(DIAL_LOGIN_IP_MAX_FAILURES=3, DIAL_LOGIN_MAX_FAILURES=50)
def test_login_ip_lockout(client):
    url = reverse("accounts:login")
    for i in range(3):
        client.post(url, {"username": f"u{i}@example.org", "password": "nope"})
    r = client.post(url, {"username": "someone-else@example.org", "password": "nope"})
    assert r.status_code == 429


# --- antispam primitives ---------------------------------------------------------------------
def test_counter_lock_clear():
    assert antispam.hit("k", 60) == 1 and antispam.hit("k", 60) == 2
    assert antispam.locked_for("k") == 0
    antispam.lock("k", 120)
    assert 100 < antispam.locked_for("k") <= 120
    antispam.clear("k")
    assert antispam.locked_for("k") == 0 and antispam.hit("k", 60) == 1


# --- domain allow / block lists --------------------------------------------------------------
@override_settings(DIAL_SIGNUP_BLOCKED_DOMAINS=["mailinator.com", "@Trash.example"])
def test_blocked_domains_including_subdomains():
    assert validate_signup_email("ok@example.org") == "ok@example.org"
    for bad in ("a@mailinator.com", "a@MAILINATOR.COM", "a@sub.mailinator.com", "a@trash.example"):
        with pytest.raises(forms.ValidationError, match="not accepted"):
            validate_signup_email(bad)


@override_settings(DIAL_SIGNUP_ALLOWED_DOMAINS=["example.org"])
def test_allowed_domains(client):
    assert validate_signup_email("a@example.org") and validate_signup_email("a@mail.example.org")
    with pytest.raises(forms.ValidationError, match="limited to these e-mail domains: example.org"):
        validate_signup_email("a@example.com")
    r = client.post(reverse("accounts:register"), {**SIGNUP, "email": "new@example.com"})
    assert r.status_code == 200 and b"limited to these e-mail domains" in r.content


# --- forced verification: extension gate + banner -------------------------------------------
@override_settings(DIAL_REQUIRE_EMAIL_VERIFICATION=True)
def test_unverified_user_cannot_register_extension(event, user, orga):
    assert not user.email_verified
    with pytest.raises(services.ExtensionError, match="verify your e-mail"):
        services.register(event, user, "4242", ExtensionType.DECT)
    user.email_verified = True
    user.save(update_fields=["email_verified"])
    assert services.register(event, user, "4242", ExtensionType.DECT).number == "4242"
    # orga and orga-driven flows are never gated
    assert services.register(event, orga, "1234", ExtensionType.SIP).number == "1234"
    user.email_verified = False
    user.save(update_fields=["email_verified"])
    assert services.register(event, user, "4343", ExtensionType.DECT, force_active=True).number == "4343"


@override_settings(DIAL_REQUIRE_EMAIL_VERIFICATION=False)
def test_gate_off_when_verification_not_required(event, user):
    assert services.register(event, user, "4242", ExtensionType.DECT).number == "4242"


def test_banner_for_unverified_user(client, user, event):
    client.force_login(user)
    r = client.get(reverse("portal:dashboard"))
    assert r.status_code == 200
    assert b"not verified yet" in r.content and reverse("accounts:resend_verification").encode() in r.content
    user.email_verified = True
    user.save(update_fields=["email_verified"])
    r = client.get(reverse("portal:dashboard"))
    assert b"not verified yet" not in r.content


def test_no_banner_for_anonymous(client):
    r = client.get(reverse("accounts:login"))
    assert b"not verified yet" not in r.content
