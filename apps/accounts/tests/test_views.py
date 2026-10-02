"""Account view tests: register/login, profile, tokens, GDPR."""
import json

import pytest
from django.urls import reverse

from apps.accounts.models import ServiceAccount, User
from apps.core.models import AuditLog
from apps.extensions import services
from apps.extensions.models import Extension

pytestmark = pytest.mark.django_db


def test_register_and_login_flow(client):
    assert client.get(reverse("accounts:register")).status_code == 200
    r = client.post(reverse("accounts:register"), {
        "email": "new@example.org", "username": "newbie",
        "password1": "a-very-strong-pw-42", "password2": "a-very-strong-pw-42"})
    assert r.status_code == 302 and r.url == reverse("portal:dashboard")
    user = User.objects.get(email="new@example.org")
    assert AuditLog.objects.filter(actor=user, action="login").exists()
    assert client.get(reverse("portal:dashboard")).status_code == 200  # auto-logged in
    client.post(reverse("accounts:logout"))
    assert client.get(reverse("accounts:login")).status_code == 200
    r = client.post(reverse("accounts:login"), {"username": "new@example.org", "password": "a-very-strong-pw-42"})
    assert r.status_code == 302
    assert client.get(reverse("portal:dashboard")).status_code == 200


def test_register_duplicate_email_rejected(client, user):
    r = client.post(reverse("accounts:register"), {
        "email": "alice@example.org", "username": "alice2",
        "password1": "a-very-strong-pw-42", "password2": "a-very-strong-pw-42"})
    assert r.status_code == 200 and b"already exists" in r.content


def test_login_wrong_password(client, user):
    r = client.post(reverse("accounts:login"), {"username": "alice@example.org", "password": "nope"})
    assert r.status_code == 200 and b"errorlist" in r.content or b"alert-error" in r.content


def test_profile_edit(client, user, member):
    client.force_login(user)
    r = client.get(reverse("accounts:profile"))
    assert r.status_code == 200 and b"Demo Camp" in r.content
    r = client.post(reverse("accounts:profile"), {"display_name": "Alice A.", "email": "alice@example.org"})
    assert r.status_code == 302
    user.refresh_from_db()
    assert user.display_name == "Alice A."


def test_password_change_and_reset_pages(client, user):
    client.force_login(user)
    r = client.post(reverse("accounts:password_change"), {"old_password": "pw-alice-1234",
                                                          "new_password1": "brand-new-pw-987",
                                                          "new_password2": "brand-new-pw-987"})
    assert r.status_code == 302
    user.refresh_from_db()
    assert user.check_password("brand-new-pw-987")
    client.logout()
    assert client.get(reverse("accounts:password_reset")).status_code == 200
    r = client.post(reverse("accounts:password_reset"), {"email": "alice@example.org"})
    assert r.status_code == 302 and r.url == reverse("accounts:password_reset_done")
    from django.core import mail

    assert len(mail.outbox) == 1 and "reset" in mail.outbox[0].subject.lower()


def test_tokens_create_shows_once_and_api_accepts(client, user, member, event):
    client.force_login(user)
    r = client.post(reverse("accounts:tokens"), {"name": "cli", "scopes": "extensions:read", "event": event.pk})
    assert r.status_code == 200
    acct = ServiceAccount.objects.get(owner=user)
    raw = [line for line in r.content.decode().splitlines() if "dial_" in line][0]
    raw = raw.split("dial_", 1)[1].split("<")[0]
    raw = "dial_" + raw
    assert ServiceAccount.hash_token(raw) == acct.token_hash
    # listing afterwards does not contain the raw token
    r2 = client.get(reverse("accounts:tokens"))
    assert raw.encode() not in r2.content and acct.token_prefix.encode() in r2.content
    client.logout()
    r = client.get("/api/v1/me/", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code == 200
    assert r.json().get("username") == "alice" or "alice" in json.dumps(r.json())
    # revoke
    client.force_login(user)
    client.post(reverse("accounts:tokens"), {"action": "revoke", "pk": acct.pk})
    acct.refresh_from_db()
    assert not acct.is_active
    client.logout()
    assert client.get("/api/v1/me/", HTTP_AUTHORIZATION=f"Bearer {raw}").status_code in (401, 403)


def test_gdpr_export_json(client, user, member, event):
    ext = services.register(event, user, "4711", "dect")
    client.force_login(user)
    r = client.get(reverse("accounts:gdpr_export"))
    assert r.status_code == 200 and r["Content-Type"] == "application/json"
    data = json.loads(r.content)
    assert data["user"]["email"] == "alice@example.org"
    assert data["memberships"][0]["event"] == "demo"
    assert data["extensions"][0]["number"] == ext.number
    assert any(a["action"] == "create" for a in data["audit"])
    assert "stats" in data


def test_gdpr_delete(client, user, member, event):
    ext = services.register(event, user, "4711", "dect")
    client.force_login(user)
    assert client.get(reverse("accounts:gdpr_delete")).status_code == 200
    r = client.post(reverse("accounts:gdpr_delete"), {})  # confirm missing
    assert r.status_code == 200
    r = client.post(reverse("accounts:gdpr_delete"), {"confirm": "on"})
    assert r.status_code == 302
    user.refresh_from_db()
    ext.refresh_from_db()
    assert not user.is_active and user.email.startswith("deleted-") and user.username.startswith("deleted-")
    assert ext.state == Extension.State.DELETED
    assert not user.has_usable_password()
    assert client.get(reverse("portal:dashboard")).status_code == 302  # logged out
