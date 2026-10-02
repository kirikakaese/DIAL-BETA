"""``manage.py dial_token`` - mint a service-account token for the REST API / ``dial`` CLI.

    manage.py dial_token --user admin@dial.local --name badge-printer --event demo \
        --scopes extensions:read phonebook:read --expires-days 14

The raw token (``dial_...``) is printed exactly once; DIAL only stores its SHA-256 hash.
"""

from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Create a ServiceAccount for a user and print its Bearer token (once)."

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True, help="Owner e-mail or nickname.")
        parser.add_argument("--name", required=True, help="Descriptive name, e.g. 'badge-printer'.")
        parser.add_argument("--event", default=None, help="Restrict the token to this event slug.")
        parser.add_argument(
            "--scopes",
            nargs="*",
            default=[],
            help="Scopes such as extensions:read phonebook:read (space- or comma-separated; empty = all owner rights).",
        )
        parser.add_argument(
            "--expires-days", type=int, default=None, help="Token lifetime in days."
        )
        parser.add_argument("--description", default="")
        parser.add_argument(
            "--export",
            action="store_true",
            help="Print 'export DIAL_TOKEN=...' instead of a human-readable message.",
        )

    def handle(self, *args, **opts):
        from django.db.models import Q

        from apps.accounts.models import ServiceAccount, User
        from apps.events.models import Event

        ident = opts["user"]
        user = User.objects.filter(Q(email__iexact=ident) | Q(username=ident)).first()
        if user is None:
            raise CommandError(f"No user with e-mail or nickname {ident!r}")
        event = None
        if opts["event"]:
            event = Event.objects.filter(slug=opts["event"]).first()
            if event is None:
                raise CommandError(f"No event with slug {opts['event']!r}")
        expires_at = None
        if opts["expires_days"]:
            expires_at = timezone.now() + dt.timedelta(days=opts["expires_days"])
        scopes = [s for chunk in opts["scopes"] for s in chunk.split(",") if s.strip()]
        acct, raw = ServiceAccount.issue(
            name=opts["name"],
            owner=user,
            event=event,
            scopes=scopes,
            expires_at=expires_at,
            description=opts["description"],
        )
        if opts["export"]:
            self.stdout.write(f"export DIAL_TOKEN={raw}")
            return
        self.stdout.write(
            self.style.SUCCESS(f"Service account '{acct.name}' created for {user.email}")
        )
        self.stdout.write(f"  event:   {event.slug if event else '(global)'}")
        self.stdout.write(
            f"  scopes:  {', '.join(acct.scopes) if acct.scopes else '(all of owner)'}"
        )
        self.stdout.write(
            f"  expires: {expires_at.isoformat(timespec='minutes') if expires_at else 'never'}"
        )
        self.stdout.write("")
        self.stdout.write("  Token (shown once - store it now):")
        self.stdout.write(f"  {raw}")
        self.stdout.write("")
        self.stdout.write('  Usage: curl -H "Authorization: Bearer <token>" $DIAL_URL/api/v1/me/')
