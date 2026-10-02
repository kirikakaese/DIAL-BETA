"""OpenID Connect login: flow start, callback validation, account linking, profile link/unlink, SSO-only mode."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse

from apps.accounts import oidc
from apps.accounts.models import User
from apps.core.models import AuditLog

pytestmark = pytest.mark.django_db

ISSUER = "https://id.example.org/realms/pet"
CLIENT_ID = "pet-client"
DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": ISSUER + "/protocol/openid-connect/auth",
    "token_endpoint": ISSUER + "/protocol/openid-connect/token",
    "userinfo_endpoint": ISSUER + "/protocol/openid-connect/userinfo",
    "end_session_endpoint": ISSUER + "/protocol/openid-connect/logout",
    "jwks_uri": ISSUER + "/protocol/openid-connect/certs",
    "code_challenge_methods_supported": ["S256"],
    "claims_supported": ["sub", "email", "email_verified", "preferred_username", "name"],
}
OIDC_ON = dict(PET_OIDC_ENABLED=True, PET_OIDC_ISSUER=ISSUER, PET_OIDC_CLIENT_ID=CLIENT_ID,
               PET_OIDC_CLIENT_SECRET="s3cret", PET_OIDC_SCOPES="openid email profile",
               PET_OIDC_AUTO_CREATE=True, PET_OIDC_TRUST_EMAIL_VERIFIED=True, PET_OIDC_ALLOW_PASSWORD_LOGIN=True,
               PET_OIDC_USERNAME_CLAIM="preferred_username", PET_OIDC_LOGOUT_AT_IDP=False,
               PET_OIDC_BUTTON_LABEL="Log in with SSO")


def oidc_settings(**overrides):
    return override_settings(**{**OIDC_ON, **overrides})


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_id_token(**overrides) -> str:
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "user-123", "exp": now + 300, "iat": now,
               "nonce": overrides.pop("nonce", "NONCE"), "email": "sso@example.org", "email_verified": True,
               "preferred_username": "ssouser", "name": "Sso User"}
    payload.update(overrides)
    header = b64url(json.dumps({"alg": "RS256", "kid": "x"}).encode())
    return f"{header}.{b64url(json.dumps(payload).encode())}.{b64url(b'dummy-signature')}"


class FakeResponse:
    def __init__(self, data, status=200, text=None, headers=None):
        self._data, self.status_code, self.headers = data, status, headers or {"Content-Type": "application/json"}
        self.text = text if text is not None else json.dumps(data)

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeRequests:
    """Stand-in for the ``requests`` module: discovery, token endpoint and userinfo are scripted."""

    RequestException = requests.RequestException
    HTTPError = requests.HTTPError

    def __init__(self, discovery=DISCOVERY, userinfo=None, token_status=200):
        self.discovery = discovery
        self.userinfo = userinfo
        self.token_status = token_status
        self.calls = []
        self.id_token_overrides = {}
        self.fail_discovery = False

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        if url.endswith("/.well-known/openid-configuration"):
            if self.fail_discovery:
                raise requests.ConnectionError("down")
            return FakeResponse(self.discovery)
        if url == DISCOVERY["userinfo_endpoint"]:
            assert kw["headers"]["Authorization"] == "Bearer AT"
            return FakeResponse(self.userinfo if self.userinfo is not None else {"sub": "user-123"})
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, data=None, **kw):
        self.calls.append(("POST", url, {"data": data, **kw}))
        assert url == DISCOVERY["token_endpoint"]
        # nonce must be the one PET issued: the test passes it via ``id_token_overrides``
        nonce = self.id_token_overrides.pop("nonce", None) or self._nonce
        if self.token_status != 200:
            return FakeResponse({"error": "invalid_grant"}, status=self.token_status)
        return FakeResponse({"access_token": "AT", "token_type": "Bearer",
                             "id_token": make_id_token(nonce=nonce, **self.id_token_overrides)})


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def fake(monkeypatch):
    f = FakeRequests()
    monkeypatch.setattr(oidc, "requests", f)
    return f


def start(client, fake, next_url=None, link=False):
    """Hit ``oidc_login``, capture nonce/state from the redirect and the session; return the parsed query."""
    params = {}
    if next_url:
        params["next"] = next_url
    if link:
        r = client.post(reverse("accounts:oidc_login"), {"link": "1"})
    else:
        r = client.get(reverse("accounts:oidc_login"), params)
    assert r.status_code == 302, r.status_code
    q = {k: v[0] for k, v in parse_qs(urlparse(r.url).query).items()}
    fake._nonce = q["nonce"]
    return r.url, q


def callback(client, state, code="CODE", **extra):
    return client.get(reverse("accounts:oidc_callback"), {"state": state, "code": code, **extra})


# --- flow start ---------------------------------------------------------------------------
@oidc_settings()
def test_login_redirects_with_pkce_state_nonce(client, fake):
    url, q = start(client, fake, next_url="/e/demo/")
    assert url.startswith(DISCOVERY["authorization_endpoint"] + "?")
    assert q["client_id"] == CLIENT_ID and q["response_type"] == "code"
    assert q["redirect_uri"].endswith("/accounts/oidc/callback/")
    assert q["scope"] == "openid email profile"
    assert q["code_challenge_method"] == "S256" and len(q["state"]) > 20 and len(q["nonce"]) > 20
    flow = client.session[oidc.SESSION_FLOW_KEY]
    assert flow["state"] == q["state"] and flow["nonce"] == q["nonce"] and flow["next"] == "/e/demo/"
    expected = b64url(hashlib.sha256(flow["verifier"].encode()).digest())
    assert q["code_challenge"] == expected


@oidc_settings()
def test_unsafe_next_is_dropped(client, fake):
    start(client, fake, next_url="https://evil.example.com/")
    assert client.session[oidc.SESSION_FLOW_KEY]["next"] == ""


def test_disabled_feature_is_404(client, fake):
    assert client.get(reverse("accounts:oidc_login")).status_code == 404
    assert client.get(reverse("accounts:oidc_callback")).status_code == 404
    r = client.get(reverse("accounts:login"))
    assert r.status_code == 200 and b"oidc-login" not in r.content


@oidc_settings()
def test_discovery_unreachable_gives_friendly_error(client, fake):
    fake.fail_discovery = True
    r = client.get(reverse("accounts:oidc_login"))
    assert r.status_code == 503 and b"not reachable" in r.content


@oidc_settings()
def test_discovery_is_cached(fake):
    oidc.discovery()
    oidc.discovery()
    assert len([c for c in fake.calls if c[0] == "GET"]) == 1


@oidc_settings()
def test_discovery_issuer_mismatch_rejected(fake):
    fake.discovery = {**DISCOVERY, "issuer": "https://other.example.org"}
    with pytest.raises(oidc.OIDCError):
        oidc.discovery()


# --- happy path -----------------------------------------------------------------------------
@oidc_settings()
def test_happy_path_creates_user_and_logs_in(client, fake):
    fake.userinfo = {"sub": "user-123", "email": "SSO@example.org", "email_verified": True,
                     "preferred_username": "ssouser", "name": "Sso User", "locale": "de-DE"}
    _url, q = start(client, fake, next_url="/e/demo/")
    r = callback(client, q["state"])
    assert r.status_code == 302 and r.url == "/e/demo/"
    user = User.objects.get(email="sso@example.org")
    assert user.username == "ssouser" and user.display_name == "Sso User"
    assert user.email_verified is True
    assert user.oidc_subject == f"{ISSUER}|user-123"
    assert not user.has_usable_password()
    assert client.session["_auth_user_id"] == str(user.pk)
    assert oidc.SESSION_FLOW_KEY not in client.session
    assert AuditLog.objects.filter(actor=user, action="login", message="OIDC login").exists()
    assert AuditLog.objects.filter(actor=user, action="create", message="Account created via OIDC").exists()
    # token request: basic auth with the secret + PKCE verifier + redirect_uri
    post = [c for c in fake.calls if c[0] == "POST"][0][2]
    assert post["auth"] == (CLIENT_ID, "s3cret")
    assert post["data"]["grant_type"] == "authorization_code" and post["data"]["code"] == "CODE"
    assert post["data"]["code_verifier"] and post["data"]["redirect_uri"].endswith("/accounts/oidc/callback/")
    assert client.get(reverse("portal:dashboard")).status_code == 200


@oidc_settings(PET_OIDC_CLIENT_SECRET="")
def test_public_client_sends_no_basic_auth(client, fake):
    _url, q = start(client, fake)
    assert callback(client, q["state"]).status_code == 302
    post = [c for c in fake.calls if c[0] == "POST"][0][2]
    assert post["auth"] is None and post["data"]["client_id"] == CLIENT_ID


@oidc_settings(PET_OIDC_TRUST_EMAIL_VERIFIED=False)
def test_untrusted_email_verified_claim_leaves_user_unverified(client, fake):
    _url, q = start(client, fake)
    assert callback(client, q["state"]).status_code == 302
    assert User.objects.get(email="sso@example.org").email_verified is False


@oidc_settings()
def test_username_from_email_and_dedupe(client, fake, user):
    fake.id_token_overrides = {"preferred_username": None, "email": "alice.new@example.org"}
    fake.userinfo = {"sub": "user-123"}
    _url, q = start(client, fake)
    assert callback(client, q["state"]).status_code == 302
    assert User.objects.get(email="alice.new@example.org").username == "alice.new"
    assert oidc.unique_username("alice") == "alice-2"


# --- validation failures ------------------------------------------------------------------
@oidc_settings()
def test_state_mismatch_400(client, fake):
    start(client, fake)
    assert callback(client, "wrong-state").status_code == 400
    assert oidc.SESSION_FLOW_KEY not in client.session  # flow consumed
    assert callback(client, "anything").status_code == 400  # no flow at all
    assert not User.objects.filter(email="sso@example.org").exists()


@oidc_settings()
def test_provider_error_is_shown(client, fake):
    _url, q = start(client, fake)
    r = callback(client, q["state"], code="", error="access_denied", error_description="User cancelled")
    assert r.status_code == 400 and b"User cancelled" in r.content


@pytest.mark.parametrize("overrides", [
    pytest.param({"nonce": "other-nonce"}, id="nonce"),
    pytest.param({"aud": "someone-else"}, id="aud"),
    pytest.param({"iss": "https://evil.example.org"}, id="iss"),
    pytest.param({"exp": int(time.time()) - 600}, id="expired"),
    pytest.param({"iat": None}, id="no-iat"),
    pytest.param({"sub": ""}, id="no-sub"),
])
@oidc_settings()
def test_invalid_id_token_rejected(client, fake, overrides):
    fake.id_token_overrides = dict(overrides)
    _url, q = start(client, fake)
    r = callback(client, q["state"])
    assert r.status_code == 400
    assert not User.objects.filter(email="sso@example.org").exists()
    assert "_auth_user_id" not in client.session


@oidc_settings()
def test_multiple_aud_requires_matching_azp(fake):
    now = int(time.time())
    base = {"iss": ISSUER, "sub": "s", "exp": now + 60, "iat": now, "nonce": "n"}
    oidc.validate_id_token({**base, "aud": [CLIENT_ID, "other"], "azp": CLIENT_ID}, "n")
    with pytest.raises(oidc.OIDCError):
        oidc.validate_id_token({**base, "aud": [CLIENT_ID, "other"], "azp": "other"}, "n")
    # 60 s leeway on exp
    oidc.validate_id_token({**base, "aud": CLIENT_ID, "exp": now - 30}, "n")


@oidc_settings()
def test_token_endpoint_refusal(client, fake):
    fake.token_status = 400
    _url, q = start(client, fake)
    assert callback(client, q["state"]).status_code == 400


@oidc_settings()
def test_missing_email_claim(client, fake):
    fake.id_token_overrides = {"email": None}
    _url, q = start(client, fake)
    r = callback(client, q["state"])
    assert r.status_code == 403 and b"did not share an e-mail address" in r.content


@oidc_settings()
def test_malformed_id_token():
    with pytest.raises(oidc.OIDCError):
        oidc.decode_jwt_payload("not.a-jwt")
    with pytest.raises(oidc.OIDCError):
        oidc.decode_jwt_payload("a.!!!.c")


@oidc_settings(PET_LOGIN_IP_MAX_FAILURES=2, PET_LOGIN_LOCKOUT_MINUTES=5)
def test_failed_callbacks_count_towards_ip_lockout(client, fake):
    fake.id_token_overrides = {"aud": "nope"}
    for _ in range(2):
        _url, q = start(client, fake)
        fake.id_token_overrides = {"aud": "nope"}
        assert callback(client, q["state"]).status_code == 400
    _url, q = start(client, fake)
    assert callback(client, q["state"]).status_code == 429


# --- linking rules ----------------------------------------------------------------------------
@oidc_settings()
def test_existing_subject_logs_in_without_creating(client, fake, user):
    user.oidc_subject = f"{ISSUER}|user-123"
    user.save()
    fake.id_token_overrides = {"email": "changed@example.org"}
    _url, q = start(client, fake)
    assert callback(client, q["state"]).status_code == 302
    assert User.objects.count() == 1 and client.session["_auth_user_id"] == str(user.pk)
    user.refresh_from_db()
    assert user.email == "alice@example.org"  # e-mail is not overwritten


@oidc_settings()
def test_link_by_verified_pet_email(client, fake, user):
    user.email_verified = True
    user.save()
    fake.id_token_overrides = {"email": "Alice@Example.org", "email_verified": False}
    _url, q = start(client, fake)
    assert callback(client, q["state"]).status_code == 302
    user.refresh_from_db()
    assert user.oidc_subject == f"{ISSUER}|user-123" and User.objects.count() == 1
    assert AuditLog.objects.filter(target_id=str(user.pk), message="OIDC identity linked").exists()


@oidc_settings()
def test_link_by_idp_verified_email_marks_pet_email_verified(client, fake, user):
    assert user.email_verified is False
    fake.id_token_overrides = {"email": "alice@example.org", "email_verified": True}
    _url, q = start(client, fake)
    assert callback(client, q["state"]).status_code == 302
    user.refresh_from_db()
    assert user.oidc_subject and user.email_verified is True


@oidc_settings()
def test_refuse_link_when_neither_side_verified(client, fake, user):
    fake.id_token_overrides = {"email": "alice@example.org", "email_verified": False}
    _url, q = start(client, fake)
    r = callback(client, q["state"])
    assert r.status_code == 403 and b"not verified yet" in r.content
    user.refresh_from_db()
    assert user.oidc_subject == "" and "_auth_user_id" not in client.session


@oidc_settings()
def test_refuse_when_email_bound_to_other_subject(client, fake, user):
    user.oidc_subject = f"{ISSUER}|someone-else"
    user.email_verified = True
    user.save()
    fake.id_token_overrides = {"email": "alice@example.org"}
    _url, q = start(client, fake)
    assert callback(client, q["state"]).status_code == 403


@oidc_settings()
def test_disabled_user_cannot_log_in(client, fake, user):
    user.oidc_subject = f"{ISSUER}|user-123"
    user.is_active = False
    user.save()
    _url, q = start(client, fake)
    r = callback(client, q["state"])
    assert r.status_code == 403 and b"disabled" in r.content


@oidc_settings(PET_OIDC_AUTO_CREATE=False)
def test_no_auto_create(client, fake):
    _url, q = start(client, fake)
    r = callback(client, q["state"])
    assert r.status_code == 403 and b"sign up first" in r.content
    assert not User.objects.filter(email="sso@example.org").exists()


@oidc_settings(PET_SIGNUP_BLOCKED_DOMAINS=["example.org"])
def test_auto_create_honours_domain_policy(client, fake):
    _url, q = start(client, fake)
    assert callback(client, q["state"]).status_code == 403
    assert not User.objects.filter(email="sso@example.org").exists()


# --- profile link / unlink -------------------------------------------------------------------
@oidc_settings()
def test_link_and_unlink_from_profile(client, fake, user):
    client.force_login(user)
    r = client.get(reverse("accounts:profile"))
    assert b"Link SSO account" in r.content and b"Unlink" not in r.content
    _url, q = start(client, fake, link=True)
    assert client.session[oidc.SESSION_FLOW_KEY]["link"] is True
    fake.id_token_overrides = {"email": "alice@example.org", "email_verified": True}
    r = callback(client, q["state"])
    assert r.status_code == 302 and r.url == reverse("accounts:profile")
    user.refresh_from_db()
    assert user.oidc_subject == f"{ISSUER}|user-123" and user.email_verified is True
    r = client.get(reverse("accounts:profile"))
    assert b"Unlink" in r.content and ISSUER.encode() in r.content
    # unlink (user has a password)
    r = client.post(reverse("accounts:oidc_unlink"))
    assert r.status_code == 302
    user.refresh_from_db()
    assert user.oidc_subject == ""
    assert AuditLog.objects.filter(target_id=str(user.pk), message="OIDC identity unlinked").exists()


@oidc_settings()
def test_link_refused_when_subject_taken(client, fake, user, other_user):
    other_user.oidc_subject = f"{ISSUER}|user-123"
    other_user.save()
    client.force_login(user)
    _url, q = start(client, fake, link=True)
    r = callback(client, q["state"])
    assert r.status_code == 302 and r.url == reverse("accounts:profile")
    user.refresh_from_db()
    assert user.oidc_subject == ""


@oidc_settings()
def test_unlink_refused_without_usable_password(client, fake, user):
    user.oidc_subject = f"{ISSUER}|user-123"
    user.set_unusable_password()
    user.save()
    client.force_login(user)
    r = client.post(reverse("accounts:oidc_unlink"), follow=True)
    assert b"Set a password first" in r.content
    user.refresh_from_db()
    assert user.oidc_subject == f"{ISSUER}|user-123"


@oidc_settings()
def test_logged_in_user_without_link_flag_is_redirected(client, fake, user):
    client.force_login(user)
    r = client.get(reverse("accounts:oidc_login"))
    assert r.status_code == 302 and r.url == reverse("portal:dashboard")


# --- templates / SSO-only mode ---------------------------------------------------------------
@oidc_settings()
def test_login_page_shows_sso_button_and_password_form(client, fake):
    r = client.get(reverse("accounts:login"))
    assert r.status_code == 200
    assert b"oidc-login" in r.content and b"Log in with SSO" in r.content
    assert b'name="password"' in r.content and b"Forgot your password" in r.content
    r = client.get(reverse("accounts:register"))
    assert b"oidc-register" in r.content and b'name="email"' in r.content


@oidc_settings(PET_OIDC_ALLOW_PASSWORD_LOGIN=False)
def test_sso_only_hides_password_form(client, fake, user):
    r = client.get(reverse("accounts:login"))
    assert r.status_code == 200 and b"oidc-login" in r.content
    assert b'name="password"' not in r.content
    assert b"Forgot your password" not in r.content and b"No account yet?" not in r.content
    assert b'class="btn btn-primary">Sign up</a>' not in r.content  # header button gone in SSO-only mode
    r = client.post(reverse("accounts:login"), {"username": "alice@example.org", "password": "pw-alice-1234"})
    assert r.status_code == 403 and "_auth_user_id" not in client.session
    r = client.get(reverse("accounts:register"))
    assert r.status_code == 200 and b"oidc-register" in r.content and b'name="email"' not in r.content
    assert client.post(reverse("accounts:register"), {"email": "x@example.org"}).status_code == 403
    # superusers still reach the admin login
    assert client.get("/admin/login/").status_code == 200


# --- logout at IdP -----------------------------------------------------------------------
@oidc_settings(PET_OIDC_LOGOUT_AT_IDP=True)
def test_logout_redirects_to_end_session_endpoint(client, fake):
    _url, q = start(client, fake)
    assert callback(client, q["state"]).status_code == 302
    assert client.session[oidc.SESSION_ID_TOKEN_KEY]
    r = client.post(reverse("accounts:logout"))
    assert r.status_code == 302 and r.url.startswith(DISCOVERY["end_session_endpoint"] + "?")
    qs = {k: v[0] for k, v in parse_qs(urlparse(r.url).query).items()}
    assert qs["id_token_hint"].count(".") == 2 and qs["client_id"] == CLIENT_ID
    assert qs["post_logout_redirect_uri"].startswith("http://testserver/")
    assert "_auth_user_id" not in client.session


@oidc_settings()
def test_logout_stays_local_by_default(client, fake, user):
    client.force_login(user)
    r = client.post(reverse("accounts:logout"))
    assert r.status_code == 302 and r.url == reverse("portal:home")


# --- management command ---------------------------------------------------------------------
@oidc_settings()
def test_pet_oidc_check(fake, capsys):
    call_command("pet_oidc_check")
    out = capsys.readouterr().out
    assert "Discovery OK" in out and DISCOVERY["token_endpoint"] in out
    assert "/accounts/oidc/callback/" in out


@oidc_settings(PET_OIDC_ISSUER="")
def test_pet_oidc_check_incomplete(fake):
    with pytest.raises(CommandError):
        call_command("pet_oidc_check")


@oidc_settings()
def test_pet_oidc_check_unreachable(fake):
    fake.fail_discovery = True
    with pytest.raises(CommandError, match="Discovery failed"):
        call_command("pet_oidc_check")
