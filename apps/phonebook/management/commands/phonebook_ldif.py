"""``manage.py phonebook_ldif --event <slug> [--out file]`` - LDIF export for hardphone LDAP directories."""
import sys

from django.core.management.base import BaseCommand, CommandError

from apps.events.models import Event
from apps.phonebook import services


class Command(BaseCommand):
    help = "Export the event phonebook as LDIF (base DN ou=phonebook,dc=<slug>,dc=dial)."

    def add_arguments(self, parser):
        parser.add_argument("--event", required=True, help="event slug")
        parser.add_argument("--out", default="-", help="output file (default: stdout)")
        parser.add_argument("--format", default="ldif", choices=sorted(services.EXPORTERS),
                            help="export format (default ldif)")

    def handle(self, *args, **opts):
        event = Event.objects.filter(slug=opts["event"]).first()
        if event is None:
            raise CommandError(f"unknown event {opts['event']!r}")
        body, _ctype, _fname = services.export(event, opts["format"])
        if opts["out"] in ("-", ""):
            sys.stdout.buffer.write(body)
        else:
            with open(opts["out"], "wb") as fh:
                fh.write(body)
            self.stderr.write(f"wrote {len(body)} bytes to {opts['out']} (base DN {services.base_dn(event)})")
