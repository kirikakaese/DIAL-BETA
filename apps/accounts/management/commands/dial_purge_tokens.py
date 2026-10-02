"""``manage.py dial_purge_tokens`` - delete expired registration / e-mail confirmation tokens.

Run it from cron or a Celery beat job, e.g. once a day. Used tokens are kept until they
expire (``DIAL_EMAIL_TOKEN_TTL_HOURS``) so the admin list shows recent activity.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.accounts.models import RegistrationEmailToken


class Command(BaseCommand):
    help = "Delete expired e-mail confirmation tokens (registration, verification, address change)."

    def handle(self, *args, **opts):
        n = RegistrationEmailToken.objects.purge_expired()
        self.stdout.write(self.style.SUCCESS(f"Purged {n} expired token(s)."))
