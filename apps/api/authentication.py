"""Token authentication for service accounts (``Authorization: Bearer pet_...``)."""
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import authentication, exceptions

from apps.accounts.models import ServiceAccount


class ServiceTokenAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).decode()
        if not header:
            return None
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() not in ("bearer", "token"):
            return None
        raw = parts[1]
        if not raw.startswith("pet_"):
            return None
        acct = ServiceAccount.objects.select_related("owner", "event").filter(
            token_hash=ServiceAccount.hash_token(raw), is_active=True,
        ).first()
        if acct is None:
            raise exceptions.AuthenticationFailed(_("Invalid service token."))
        if acct.expires_at and acct.expires_at < timezone.now():
            raise exceptions.AuthenticationFailed(_("Service token expired."))
        if not acct.owner.is_active:
            raise exceptions.AuthenticationFailed(_("Owner account disabled."))
        ServiceAccount.objects.filter(pk=acct.pk).update(last_used_at=timezone.now())
        request.service_account = acct
        return (acct.owner, acct)

    def authenticate_header(self, request):
        return self.keyword
