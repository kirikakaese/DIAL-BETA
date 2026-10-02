"""OpenID Connect login - authorization code flow with PKCE, implemented on plain ``requests``.

No signature verification is performed on the ID token: it is fetched by PET directly from the token
endpoint over TLS, which OIDC Core 1.0 section 3.1.3.7 (rule 6) explicitly allows. Instead we validate
``iss``, ``aud``/``azp``, ``exp``, ``iat`` and the ``nonce`` we issued. Claims are then completed from the
UserInfo endpoint (which wins on conflicts).

Account linking rules (:func:`resolve_user`):

1. ``User.oidc_subject == "<issuer>|<sub>"`` -> that user.
2. Same e-mail address (case-insensitive): link only if the PET account has ``email_verified`` or the IdP
   asserts ``email_verified`` (and ``PET_OIDC_TRUST_EMAIL_VERIFIED`` is on) - never via an unverified address.
3. Otherwise create an account (``PET_OIDC_AUTO_CREATE``) with an unusable password.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time

import requests
from django import forms
from django.conf import settings
from django.core.cache import cache
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.utils.translation import gettext as _

from apps.core.audit import log as audit

from .models import User

log = logging.getLogger("pet.oidc")

HTTP_TIMEOUT = 5
DISCOVERY_TTL = 3600
EXP_LEEWAY = 60
FLOW_TTL = 10 * 60
SESSION_FLOW_KEY = "oidc_flow"
SESSION_ID_TOKEN_KEY = "oidc_id_token"


class OIDCError(Exception):
    """A failure whose ``str()`` is safe to show to the end user."""


# --------------------------------------------------------------------------- settings helpers

def _setting(name, default=None):
    return getattr(settings, name, default)


def issuer() -> str:
    return (_setting("PET_OIDC_ISSUER", "") or "").strip().rstrip("/")


def client_id() -> str:
    return (_setting("PET_OIDC_CLIENT_ID", "") or "").strip()


def enabled() -> bool:
    return bool(_setting("PET_OIDC_ENABLED", False)) and bool(issuer()) and bool(client_id())


def password_login_allowed() -> bool:
    """False = SSO only: the password form, signup and reset links are hidden (admin login keeps working)."""
    return not enabled() or bool(_setting("PET_OIDC_ALLOW_PASSWORD_LOGIN", True))


def template_context() -> dict:
    return {
        "oidc_enabled": enabled(),
        "oidc_button_label": _setting("PET_OIDC_BUTTON_LABEL", "Log in with SSO"),
        "oidc_password_login": password_login_allowed(),
        "oidc_issuer": issuer(),
    }


def redirect_uri(request) -> str:
    return request.build_absolute_uri(reverse("accounts:oidc_callback"))


# --------------------------------------------------------------------------- discovery

def discovery(force: bool = False) -> dict:
    """Fetch ``<issuer>/.well-known/openid-configuration`` (cached for one hour)."""
    iss = issuer()
    if not iss:
        raise OIDCError(_("Single sign-on is not configured on this server."))
    key = "oidc:discovery:" + hashlib.sha256(iss.encode()).hexdigest()[:24]
    doc = None if force else cache.get(key)
    if doc:
        return doc
    url = iss + "/.well-known/openid-configuration"
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"Accept": "application/json"})
        r.raise_for_status()
        doc = r.json()
    except (requests.RequestException, ValueError) as exc:
        log.error("OIDC discovery failed for %s: %s", url, exc)
        raise OIDCError(_("The single sign-on provider is not reachable right now. Please try again later."))
    for field in ("authorization_endpoint", "token_endpoint", "issuer"):
        if not doc.get(field):
            log.error("OIDC discovery document at %s lacks %r", url, field)
            raise OIDCError(_("The single sign-on provider is misconfigured. Please contact the operator."))
    if str(doc["issuer"]).rstrip("/") != iss:
        log.error("OIDC discovery issuer mismatch: configured %s, document says %s", iss, doc["issuer"])
        raise OIDCError(_("The single sign-on provider is misconfigured. Please contact the operator."))
    cache.set(key, doc, DISCOVERY_TTL)
    return doc


# --------------------------------------------------------------------------- flow: start

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def safe_next(request, url: str | None) -> str:
    if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return url
    return ""


def start_flow(request, next_url: str = "", link: bool = False) -> str:
    """Store state/nonce/PKCE verifier in the session and return the authorization URL."""
    doc = discovery()
    verifier = b64url(secrets.token_bytes(48))
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    flow = {
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "verifier": verifier,
        "next": safe_next(request, next_url),
        "link": bool(link),
        "ts": int(time.time()),
    }
    request.session[SESSION_FLOW_KEY] = flow
    params = {
        "response_type": "code",
        "client_id": client_id(),
        "redirect_uri": redirect_uri(request),
        "scope": _setting("PET_OIDC_SCOPES", "openid email profile") or "openid email profile",
        "state": flow["state"],
        "nonce": flow["nonce"],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    sep = "&" if "?" in doc["authorization_endpoint"] else "?"
    return doc["authorization_endpoint"] + sep + urlencode(params)


def pop_flow(request) -> dict | None:
    flow = request.session.pop(SESSION_FLOW_KEY, None)
    if not isinstance(flow, dict) or int(time.time()) - int(flow.get("ts", 0)) > FLOW_TTL:
        return None
    return flow


# --------------------------------------------------------------------------- flow: callback

def exchange_code(request, code: str, verifier: str) -> dict:
    """Redeem the authorization code at the token endpoint. Confidential clients use HTTP Basic."""
    doc = discovery()
    secret = _setting("PET_OIDC_CLIENT_SECRET", "") or ""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(request),
        "client_id": client_id(),
        "code_verifier": verifier,
    }
    try:
        r = requests.post(doc["token_endpoint"], data=data, timeout=HTTP_TIMEOUT,
                          auth=(client_id(), secret) if secret else None,
                          headers={"Accept": "application/json"})
        body = r.json()
    except (requests.RequestException, ValueError) as exc:
        log.error("OIDC token request failed: %s", exc)
        raise OIDCError(_("Could not complete the login with the single sign-on provider. Please try again."))
    if r.status_code != 200 or "error" in body or not body.get("id_token"):
        log.warning("OIDC token endpoint refused the code: status=%s error=%s desc=%s", r.status_code,
                    body.get("error"), body.get("error_description"))
        raise OIDCError(_("Could not complete the login with the single sign-on provider. Please try again."))
    return body


def decode_jwt_payload(token: str) -> dict:
    """Return the payload of a compact JWS *without* verifying the signature (see module docstring)."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        raise OIDCError(_("The single sign-on provider returned an invalid token."))
    try:
        payload = json.loads(b64url_decode(parts[1]))
    except (ValueError, UnicodeDecodeError):
        raise OIDCError(_("The single sign-on provider returned an invalid token."))
    if not isinstance(payload, dict):
        raise OIDCError(_("The single sign-on provider returned an invalid token."))
    return payload


def validate_id_token(claims: dict, nonce: str, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    bad = _("The single sign-on provider returned an invalid token.")
    if str(claims.get("iss", "")).rstrip("/") != issuer():
        log.warning("OIDC id_token iss mismatch: %r", claims.get("iss"))
        raise OIDCError(bad)
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if client_id() not in auds:
        log.warning("OIDC id_token aud mismatch: %r", aud)
        raise OIDCError(bad)
    if len(auds) > 1 and claims.get("azp") not in (None, client_id()):
        log.warning("OIDC id_token azp mismatch: %r", claims.get("azp"))
        raise OIDCError(bad)
    try:
        exp = int(claims["exp"])
        int(claims["iat"])
    except (KeyError, TypeError, ValueError):
        log.warning("OIDC id_token without exp/iat")
        raise OIDCError(bad)
    if exp + EXP_LEEWAY < now:
        log.warning("OIDC id_token expired at %s", exp)
        raise OIDCError(_("The login took too long. Please try again."))
    if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        log.warning("OIDC id_token nonce mismatch")
        raise OIDCError(bad)
    if not claims.get("sub"):
        raise OIDCError(bad)
    return claims


def fetch_userinfo(access_token: str) -> dict:
    """Claims from the UserInfo endpoint; empty dict when unavailable (the ID token is enough)."""
    doc = discovery()
    endpoint = doc.get("userinfo_endpoint")
    if not endpoint or not access_token:
        return {}
    try:
        r = requests.get(endpoint, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
                         timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            log.warning("OIDC userinfo returned %s", r.status_code)
            return {}
        if "jwt" in (r.headers.get("Content-Type") or ""):
            return decode_jwt_payload(r.text)
        data = r.json()
    except (requests.RequestException, ValueError, OIDCError) as exc:
        log.warning("OIDC userinfo failed: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def claims_from_tokens(tokens: dict, nonce: str) -> dict:
    """Validate the ID token and merge UserInfo claims on top (UserInfo wins, ``sub`` must agree)."""
    claims = validate_id_token(decode_jwt_payload(tokens["id_token"]), nonce)
    info = fetch_userinfo(tokens.get("access_token", ""))
    if info:
        if info.get("sub") not in (None, claims["sub"]):
            log.warning("OIDC userinfo sub differs from id_token sub - ignoring userinfo")
        else:
            claims = {**claims, **info}
    return claims


# --------------------------------------------------------------------------- claims -> user

def subject_key(claims: dict) -> str:
    return f"{issuer()}|{claims['sub']}"


def claim_email(claims: dict) -> str:
    email = (claims.get("email") or "").strip()
    if not email or "@" not in email:
        raise OIDCError(_("Your identity provider did not share an e-mail address. "
                          "Please allow PET to see your e-mail address or contact the operator."))
    return User.objects.normalize_email(email).lower()


def idp_email_verified(claims: dict) -> bool:
    return bool(_setting("PET_OIDC_TRUST_EMAIL_VERIFIED", True)) and claims.get("email_verified") is True


def unique_username(base: str) -> str:
    base = " ".join((base or "").split())[:64].strip() or "user"
    candidate, n = base, 1
    while User.objects.filter(username__iexact=candidate).exists():
        n += 1
        suffix = f"-{n}"
        candidate = base[:64 - len(suffix)] + suffix
    return candidate


def _display_name(claims: dict) -> str:
    name = claims.get("name") or " ".join(p for p in (claims.get("given_name"), claims.get("family_name")) if p)
    return (name or "")[:120]


def _check_active(user):
    if not user.is_active:
        raise OIDCError(_("This account is disabled."))


def resolve_user(claims: dict, request=None) -> tuple[User, str]:
    """Map validated claims to a PET user. Returns ``(user, "existing" | "linked" | "created")``."""
    key = subject_key(claims)
    email = claim_email(claims)
    verified = idp_email_verified(claims)

    user = User.objects.filter(oidc_subject=key).first()
    if user is not None:
        _check_active(user)
        if verified and not user.email_verified and user.email.lower() == email:
            user.email_verified = True
            user.save(update_fields=["email_verified"])
        return user, "existing"

    user = User.objects.filter(email__iexact=email).first()
    if user is not None:
        _check_active(user)
        if user.oidc_subject and user.oidc_subject != key:
            raise OIDCError(_("This e-mail address belongs to an account that is linked to a different "
                              "single sign-on identity."))
        if not (user.email_verified or verified):
            raise OIDCError(_("A PET account with this e-mail address exists but the address is not verified yet. "
                              "Log in with your password, verify your e-mail address and try again."))
        user.oidc_subject = key
        fields = ["oidc_subject"]
        if verified and not user.email_verified:
            user.email_verified = True
            fields.append("email_verified")
        user.save(update_fields=fields)
        audit(action="update", actor=user, target=user, request=request, message="OIDC identity linked",
              changes={"oidc_subject": ["", key]})
        return user, "linked"

    if not _setting("PET_OIDC_AUTO_CREATE", True):
        raise OIDCError(_("There is no PET account for this e-mail address yet. Please sign up first."))
    from .forms import validate_signup_email

    try:
        validate_signup_email(email)
    except forms.ValidationError as exc:
        raise OIDCError(" ".join(exc.messages))
    username_claim = _setting("PET_OIDC_USERNAME_CLAIM", "preferred_username") or "preferred_username"
    username = unique_username(str(claims.get(username_claim) or email.split("@", 1)[0]))
    user = User(email=email, username=username, display_name=_display_name(claims),
                oidc_subject=key, email_verified=verified)
    user.set_unusable_password()
    user.save()
    audit(action="create", actor=user, target=user, request=request, message="Account created via OIDC")
    return user, "created"


# --------------------------------------------------------------------------- linking / logout

def link_user(user, claims: dict, request=None) -> None:
    """Attach ``claims['sub']`` to an already logged-in user (profile 'Link SSO account')."""
    key = subject_key(claims)
    if user.oidc_subject == key:
        return
    if User.objects.filter(oidc_subject=key).exclude(pk=user.pk).exists():
        raise OIDCError(_("This single sign-on identity is already linked to another PET account."))
    old = user.oidc_subject
    user.oidc_subject = key
    fields = ["oidc_subject"]
    try:
        same_email = claim_email(claims) == user.email.lower()
    except OIDCError:
        same_email = False
    if same_email and idp_email_verified(claims) and not user.email_verified:
        user.email_verified = True
        fields.append("email_verified")
    user.save(update_fields=fields)
    audit(action="update", actor=user, target=user, request=request, message="OIDC identity linked",
          changes={"oidc_subject": [old, key]})


def end_session_url(id_token: str, post_logout_redirect: str) -> str | None:
    """RP-initiated logout URL (``PET_OIDC_LOGOUT_AT_IDP``) or ``None`` when unsupported/unreachable."""
    if not (enabled() and _setting("PET_OIDC_LOGOUT_AT_IDP", False) and id_token):
        return None
    try:
        endpoint = discovery().get("end_session_endpoint")
    except OIDCError:
        return None
    if not endpoint:
        return None
    params = {"id_token_hint": id_token, "post_logout_redirect_uri": post_logout_redirect, "client_id": client_id()}
    return endpoint + ("&" if "?" in endpoint else "?") + urlencode(params)
