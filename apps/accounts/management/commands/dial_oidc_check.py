"""``manage.py dial_oidc_check`` - verify the OpenID Connect configuration.

Fetches the discovery document of ``DIAL_OIDC_ISSUER`` (bypassing the cache), prints the endpoints DIAL will use,
the redirect URI to register at the identity provider and the relevant ``DIAL_OIDC_*`` settings. Exits non-zero
when SSO is disabled, incomplete or the provider is unreachable.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from apps.accounts import oidc


class Command(BaseCommand):
    help = "Check the OpenID Connect (SSO) configuration and print the provider's endpoints."

    def handle(self, *args, **opts):
        out = self.stdout
        issuer = oidc.issuer()
        client_id = oidc.client_id()
        out.write(f"DIAL_OIDC_ENABLED            : {getattr(settings, 'DIAL_OIDC_ENABLED', False)}")
        out.write(f"DIAL_OIDC_ISSUER             : {issuer or '(unset)'}")
        out.write(f"DIAL_OIDC_CLIENT_ID          : {client_id or '(unset)'}")
        secret_state = "set" if getattr(settings, "DIAL_OIDC_CLIENT_SECRET", "") else "empty (public client)"
        out.write(f"DIAL_OIDC_CLIENT_SECRET      : {secret_state}")
        out.write(f"DIAL_OIDC_SCOPES             : {getattr(settings, 'DIAL_OIDC_SCOPES', '')}")
        out.write(f"DIAL_OIDC_USERNAME_CLAIM     : {getattr(settings, 'DIAL_OIDC_USERNAME_CLAIM', '')}")
        out.write(f"DIAL_OIDC_AUTO_CREATE        : {getattr(settings, 'DIAL_OIDC_AUTO_CREATE', True)}")
        out.write(f"DIAL_OIDC_TRUST_EMAIL_VERIFIED: {getattr(settings, 'DIAL_OIDC_TRUST_EMAIL_VERIFIED', True)}")
        out.write(f"DIAL_OIDC_ALLOW_PASSWORD_LOGIN: {getattr(settings, 'DIAL_OIDC_ALLOW_PASSWORD_LOGIN', True)}")
        out.write(f"DIAL_OIDC_LOGOUT_AT_IDP      : {getattr(settings, 'DIAL_OIDC_LOGOUT_AT_IDP', False)}")
        redirect_uri = settings.DIAL_PUBLIC_URL.rstrip("/") + reverse("accounts:oidc_callback")
        out.write(f"Redirect URI (register at IdP): {redirect_uri}")
        if not issuer or not client_id:
            raise CommandError("DIAL_OIDC_ISSUER and DIAL_OIDC_CLIENT_ID must be set.")
        try:
            doc = oidc.discovery(force=True)
        except oidc.OIDCError as exc:
            raise CommandError(f"Discovery failed: {exc}")
        out.write("")
        out.write(self.style.SUCCESS(f"Discovery OK: {issuer}/.well-known/openid-configuration"))
        for key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint", "end_session_endpoint",
                    "jwks_uri"):
            out.write(f"  {key:<24}: {doc.get(key) or '-'}")
        if not doc.get("userinfo_endpoint"):
            out.write(self.style.WARNING("  no userinfo_endpoint: claims come from the ID token only"))
        if getattr(settings, "DIAL_OIDC_LOGOUT_AT_IDP", False) and not doc.get("end_session_endpoint"):
            out.write(self.style.WARNING(
                "  DIAL_OIDC_LOGOUT_AT_IDP is on but the provider has no end_session_endpoint"
            ))
        if "S256" not in (doc.get("code_challenge_methods_supported") or ["S256"]):
            out.write(self.style.WARNING("  provider does not advertise PKCE S256 - DIAL always sends it"))
        wanted = {"sub", "email", "email_verified", getattr(settings, "DIAL_OIDC_USERNAME_CLAIM", ""), "name"}
        advertised = set(doc.get("claims_supported") or [])
        if advertised:
            missing = sorted(c for c in wanted if c and c not in advertised)
            out.write(f"  claims_supported        : {', '.join(sorted(advertised))}")
            if missing:
                out.write(self.style.WARNING(f"  claims not advertised: {', '.join(missing)}"))
        if not getattr(settings, "DIAL_OIDC_ENABLED", False):
            out.write(self.style.WARNING("DIAL_OIDC_ENABLED is off - the SSO button is hidden."))
