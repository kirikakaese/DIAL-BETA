"""E-mail confirmation flows: e-mail-first signup, post-signup verification, address change, limiter."""
import datetime as dt
import re

import pytest
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts import tokens as token_mails
from apps.accounts.models import RegistrationEmailToken, TokenError, User
from apps.core.models import AuditLog

pytestmark = pytest.mark.django_db

PW = "a-very-strong-pw-42"


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


def _link(msg, url_name):
    """Extract the raw token from the first ``url_name`` link in a mail body."""
    prefix = reverse(url_name, args=["TOKEN"]).split("TOKEN")[0]
    m = re.search(re.escape(prefix) + r"([A-Za-z0-9_\-]+)/", msg.body)
    assert m, f"no {url_name} link in mail:\n{msg.body}"
    return m.group(1)


# --- model ------------------------------------------------------------------
def test_issue_and_redeem_token():
    tok, raw = RegistrationEmailToken.issue("Someone@Example.ORG", "register")
    assert tok.email == "someone@example.org"
    assert tok.token_hash == RegistrationEmailToken.hash_token(raw) and raw not in tok.token_hash
    assert tok.is_valid
    assert RegistrationEmailToken.redeem(raw).pk == tok.pk
    tok.refresh_from_db()
    assert tok.used_at is not None and not tok.is_valid
    with pytest.raises(TokenError, match="used"):
        RegistrationEmailToken.redeem(raw)
    with pytest.raises(TokenError, match="invalid"):
        RegistrationEmailToken.redeem("nope")
    with pytest.raises(TokenError, match="invalid"):
        RegistrationEmailToken.redeem("")


def test_expired_token_and_purge():
    tok, raw = RegistrationEmailToken.issue("x@example.org", "verify")
    tok.expires_at = timezone.now() - dt.timedelta(minutes=1)
    tok.save()
    with pytest.raises(TokenError, match="expired"):
        RegistrationEmailToken.lookup(raw)
    RegistrationEmailToken.issue("y@example.org", "verify")
    assert RegistrationEmailToken.objects.purge_expired() == 1
    assert RegistrationEmailToken.objects.count() == 1


def test_purge_command(capsys):
    from django.core.management import call_command

    tok, _ = RegistrationEmailToken.issue("x@example.org", "verify")
    RegistrationEmailToken.objects.filter(pk=tok.pk).update(expires_at=timezone.now() - dt.timedelta(days=1))
    call_command("dial_purge_tokens")
    assert "Purged 1" in capsys.readouterr().out
    assert not RegistrationEmailToken.objects.exists()


# --- e-mail-first registration (setting on) -----------------------------------
@override_settings(DIAL_REQUIRE_EMAIL_VERIFICATION=True, DIAL_PUBLIC_URL="https://dial.example.org/")
def test_email_first_registration_flow(client):
    r = client.get(reverse("accounts:register"))
    assert r.status_code == 200
    assert b'name="email"' in r.content and b"password1" not in r.content

    r = client.post(reverse("accounts:register"), {"email": "New@Example.org"})
    assert r.status_code == 200 and b"Check your inbox" in r.content
    assert not User.objects.filter(email__iexact="new@example.org").exists()
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.to == ["new@example.org"] and msg.subject.startswith("[DIAL] ")
    assert "https://dial.example.org/accounts/register/confirm/" in msg.body
    raw = _link(msg, "accounts:register_confirm")
    tok = RegistrationEmailToken.objects.get(email="new@example.org", purpose="register")
    assert tok.token_hash == RegistrationEmailToken.hash_token(raw) and tok.ip == "127.0.0.1"

    url = reverse("accounts:register_confirm", args=[raw])
    r = client.get(url)
    assert r.status_code == 200 and b"new@example.org" in r.content and b"password1" in r.content

    r = client.post(url, {"username": "newbie", "password1": PW, "password2": PW})
    assert r.status_code == 302 and r.url == reverse("portal:dashboard")
    user = User.objects.get(email="new@example.org")
    assert user.email_verified and user.username == "newbie"
    assert user.check_password(PW)
    assert AuditLog.objects.filter(actor=user, action="create", target_id=str(user.pk)).exists()
    assert client.get(reverse("portal:dashboard")).status_code == 200  # logged in
    tok.refresh_from_db()
    assert tok.used_at is not None and tok.user == user

    # token cannot be used again
    client.logout()
    r = client.get(url)
    assert r.status_code == 400 and b"already been used" in r.content


@override_settings(DIAL_REQUIRE_EMAIL_VERIFICATION=True)
def test_expired_register_token_rejected(client):
    tok, raw = RegistrationEmailToken.issue("late@example.org", "register")
    RegistrationEmailToken.objects.filter(pk=tok.pk).update(expires_at=timezone.now() - dt.timedelta(hours=1))
    url = reverse("accounts:register_confirm", args=[raw])
    assert client.get(url).status_code == 400
    r = client.post(url, {"username": "late", "password1": PW, "password2": PW})
    assert r.status_code == 400 and b"expired" in r.content
    assert not User.objects.filter(email="late@example.org").exists()
    assert client.get(reverse("accounts:register_confirm", args=["garbage"])).status_code == 400


@override_settings(DIAL_REQUIRE_EMAIL_VERIFICATION=True)
def test_register_existing_email_reveals_nothing(client, user):
    r1 = client.post(reverse("accounts:register"), {"email": "alice@example.org"})
    r2 = client.post(reverse("accounts:register"), {"email": "fresh@example.org"})
    assert r1.status_code == r2.status_code == 200
    assert b"already" not in r1.content
    # same page for both, apart from the echoed address (and the per-request CSRF token)
    def norm(content, addr):
        return re.sub(rb'value="[^"]{32,}"', b"", content.replace(addr, b"X"))

    assert norm(r1.content, b"alice@example.org") == norm(r2.content, b"fresh@example.org")
    assert len(mail.outbox) == 2
    exists_mail = mail.outbox[0]
    assert exists_mail.to == ["alice@example.org"] and "already has an account" in exists_mail.body
    assert reverse("accounts:password_reset") in exists_mail.body
    assert "register/confirm/" not in exists_mail.body
    assert not RegistrationEmailToken.objects.filter(email="alice@example.org").exists()
    assert RegistrationEmailToken.objects.filter(email="fresh@example.org", purpose="register").exists()


@override_settings(DIAL_REQUIRE_EMAIL_VERIFICATION=True)
def test_register_confirm_rejects_if_account_created_meanwhile(client):
    _, raw = RegistrationEmailToken.issue("race@example.org", "register")
    User.objects.create_user(email="race@example.org", username="racer", password=PW)
    r = client.get(reverse("accounts:register_confirm", args=[raw]))
    assert r.status_code == 400 and b"already exists" in r.content


# --- one-step registration + verify mail (setting off) ------------------------
@override_settings(DIAL_REQUIRE_EMAIL_VERIFICATION=False)
def test_one_step_registration_sends_verify_mail(client):
    r = client.post(reverse("accounts:register"), {
        "email": "quick@example.org", "username": "quick", "password1": PW, "password2": PW})
    assert r.status_code == 302
    user = User.objects.get(email="quick@example.org")
    assert not user.email_verified
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["quick@example.org"]
    assert "Verify" in mail.outbox[0].subject

    r = client.get(reverse("accounts:profile"))
    assert b"unverified" in r.content and reverse("accounts:resend_verification").encode() in r.content

    raw = _link(mail.outbox[0], "accounts:verify_email")
    r = client.get(reverse("accounts:verify_email", args=[raw]))
    assert r.status_code == 302 and r.url == reverse("accounts:profile")
    user.refresh_from_db()
    assert user.email_verified
    r = client.get(reverse("accounts:profile"))
    assert b">verified<" in r.content and b"unverified" not in r.content
    # second use fails
    assert client.get(reverse("accounts:verify_email", args=[raw])).status_code == 400


def test_resend_verification(client, user):
    client.force_login(user)
    r = client.post(reverse("accounts:resend_verification"))
    assert r.status_code == 302 and len(mail.outbox) == 1
    raw = _link(mail.outbox[0], "accounts:verify_email")
    client.logout()
    r = client.get(reverse("accounts:verify_email", args=[raw]))  # works logged-out too
    assert r.status_code == 302 and r.url == reverse("accounts:login")
    user.refresh_from_db()
    assert user.email_verified
    client.force_login(user)
    client.post(reverse("accounts:resend_verification"))
    assert len(mail.outbox) == 1  # already verified: nothing sent


def test_verify_token_for_changed_address_is_rejected(client, user):
    _, raw = RegistrationEmailToken.issue(user.email, "verify", user=user)
    user.email = "moved@example.org"
    user.save()
    assert client.get(reverse("accounts:verify_email", args=[raw])).status_code == 400


# --- e-mail change -------------------------------------------------------------
def test_change_email_flow(client, user, other_user):
    client.force_login(user)
    # taken address and own address are rejected by the form
    r = client.post(reverse("accounts:change_email"), {"new_email": "bob@example.org"})
    assert r.status_code == 200 and b"already exists" in r.content and not mail.outbox
    r = client.post(reverse("accounts:change_email"), {"new_email": "alice@example.org"})
    assert r.status_code == 200 and b"already your" in r.content

    r = client.post(reverse("accounts:change_email"), {"new_email": "Alice.New@Example.org"})
    assert r.status_code == 302
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["alice.new@example.org"]
    user.refresh_from_db()
    assert user.email == "alice@example.org"  # unchanged until confirmed

    raw = _link(mail.outbox[0], "accounts:change_email_confirm")
    r = client.get(reverse("accounts:change_email_confirm", args=[raw]))
    assert r.status_code == 302
    user.refresh_from_db()
    assert user.email == "alice.new@example.org" and user.email_verified
    # old address notified
    assert len(mail.outbox) == 2 and mail.outbox[1].to == ["alice@example.org"]
    assert "alice.new@example.org" in mail.outbox[1].body
    entry = AuditLog.objects.filter(actor=user, action="update", message__icontains="changed").first()
    assert entry and entry.changes == {"email": ["alice@example.org", "alice.new@example.org"]}
    assert client.get(reverse("accounts:change_email_confirm", args=[raw])).status_code == 400


def test_change_email_confirm_checks_uniqueness_at_redeem(client, user):
    _, raw = RegistrationEmailToken.issue("taken@example.org", "change_email", user=user, new_email="taken@example.org")
    User.objects.create_user(email="taken@example.org", username="taker", password=PW)
    r = client.get(reverse("accounts:change_email_confirm", args=[raw]))
    assert r.status_code == 400
    user.refresh_from_db()
    assert user.email == "alice@example.org"


# --- rate limiting ----------------------------------------------------------------
@override_settings(DIAL_REQUIRE_EMAIL_VERIFICATION=True)
def test_token_mail_rate_limit_per_email(client):
    for _ in range(token_mails.TOKEN_MAILS_PER_EMAIL + 2):
        r = client.post(reverse("accounts:register"), {"email": "spam@example.org"})
        assert r.status_code == 200 and b"Check your inbox" in r.content  # same page when limited
    assert len(mail.outbox) == token_mails.TOKEN_MAILS_PER_EMAIL
    # other addresses from the same IP still work (IP limit is higher)
    client.post(reverse("accounts:register"), {"email": "other@example.org"})
    assert len(mail.outbox) == token_mails.TOKEN_MAILS_PER_EMAIL + 1


def test_token_mail_rate_limit_per_ip(rf):
    request = rf.post("/", REMOTE_ADDR="10.9.8.7")
    sent = sum(token_mails.token_mail_allowed(f"u{i}@example.org", request)
               for i in range(token_mails.TOKEN_MAILS_PER_IP + 5))
    assert sent == token_mails.TOKEN_MAILS_PER_IP
    assert token_mails.token_mail_allowed("u0@example.org", rf.post("/", REMOTE_ADDR="10.0.0.1"))
