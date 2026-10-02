"""``manage.py pet_a11y`` - run the built-in accessibility linter against rendered pages.

    manage.py pet_a11y                 # smoke-test URL list, logged in as the demo admin
    manage.py pet_a11y --all           # also the pages from the anonymous / user lists as those personas
    manage.py pet_a11y --url /events/ --url /e/demo/orga/

Needs the seeded dev database (``manage.py seed_demo``). Prints one line per finding
(``<rule>: <element> (line N)``) and exits non-zero when anything was found. The same checks run in
``apps/core/tests/test_a11y.py`` - this command is the interactive variant for template work.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.test import Client

from apps.core.a11y import audit_url, smoke_page_lists


class Command(BaseCommand):
    help = "Check rendered pages for accessibility problems (labels, alt text, headings, landmarks, ...)."

    def add_arguments(self, parser):
        parser.add_argument("--url", action="append", default=[], help="Check only this URL (repeatable).")
        parser.add_argument("--all", action="store_true",
                            help="Also check the anonymous and user URL lists as anonymous / alice.")
        parser.add_argument("--event", default="demo", help="Event slug (default: demo).")
        parser.add_argument("--admin", default="admin@pet.local", help="Admin e-mail to log in with.")
        parser.add_argument("--user", default="alice@pet.local", help="Regular user e-mail for --all.")

    def handle(self, *args, **opts):
        from apps.accounts.models import User
        from apps.devices.models import Device
        from apps.events.models import Event
        from apps.extensions.models import Extension
        from apps.phonebook import remote as pb_remote
        from apps.phonebook import services as pb_services

        event = Event.objects.filter(slug=opts["event"]).first()
        admin = User.objects.filter(email__iexact=opts["admin"]).first()
        if event is None or admin is None:
            raise CommandError("Event or admin user not found - run `manage.py seed_demo` first.")
        alice = User.objects.filter(email__iexact=opts["user"]).first() or admin
        ext = Extension.objects.filter(event=event, owner=alice, state="active").first() \
            or Extension.objects.filter(event=event, state="active").first()
        dev = Device.objects.filter(event=event, owner=alice).first() or Device.objects.filter(event=event).first()

        admin_client = Client()
        admin_client.force_login(admin)
        runs: list[tuple[str, Client, list[str]]] = []
        if opts["url"]:
            runs.append(("admin", admin_client, opts["url"]))
        else:
            pages = smoke_page_lists(
                S=event.slug, ext_pk=ext.pk if ext else 0, ext_number=ext.number if ext else "0",
                dev_pk=dev.pk if dev else 0, pb_token=pb_services.get_settings(event).directory_token,
                vendors=pb_remote.VENDORS,
            )
            runs.append(("admin", admin_client, pages["orga"]))
            if opts["all"]:
                user_client = Client()
                user_client.force_login(alice)
                runs.append(("anon", Client(), pages["anon"]))
                runs.append(("user", user_client, pages["user"]))

        total = 0
        verbose = opts.get("verbosity", 1) > 1
        for who, client, urls in runs:
            for url in urls:
                findings = audit_url(client, url)
                if findings is None:
                    continue
                if findings:
                    total += len(findings)
                    self.stdout.write(self.style.ERROR(f"FAIL [{who}] {url}"))
                    for f in findings:
                        self.stdout.write(f"    {f}")
                elif verbose:
                    self.stdout.write(f"ok   [{who}] {url}")
        if total:
            raise CommandError(f"{total} accessibility finding(s)")
        self.stdout.write(self.style.SUCCESS("No accessibility findings."))
