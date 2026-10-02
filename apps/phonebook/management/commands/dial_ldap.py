"""``manage.py dial_ldap [--host H] [--port P] [--cert crt.pem [--key key.pem]]`` - read-only LDAP phonebook server.

Serves every registration/live event with ``directory_enabled`` below ``dc=<slug>,dc=dial``; phones bind as
``cn=directory,dc=<slug>,dc=dial`` with the event's directory token. ``--cert`` switches to LDAPS.
Stops cleanly on SIGTERM/SIGINT.
"""
import asyncio
import ssl

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.phonebook.ldap.server import serve


class Command(BaseCommand):
    help = "Serve the event phonebooks over LDAP v3 (read-only) for desk phones and the DECT OMM."

    def add_arguments(self, parser):
        parser.add_argument("--host", default=settings.DIAL_LDAP_HOST, help="bind address (DIAL_LDAP_HOST)")
        parser.add_argument("--port", type=int, default=settings.DIAL_LDAP_PORT, help="TCP port (DIAL_LDAP_PORT)")
        parser.add_argument("--cert", help="PEM certificate (chain) - enables LDAPS on the same port")
        parser.add_argument("--key", help="PEM private key (default: taken from --cert)")

    def handle(self, *args, **opts):
        ssl_context = None
        if opts["key"] and not opts["cert"]:
            raise CommandError("--key requires --cert")
        if opts["cert"]:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
            try:
                ssl_context.load_cert_chain(opts["cert"], opts["key"] or None)
            except (OSError, ssl.SSLError) as exc:
                raise CommandError(f"cannot load TLS certificate: {exc}")
        scheme = "ldaps" if ssl_context else "ldap"
        self.stdout.write(f"DIAL LDAP phonebook listening on {scheme}://{opts['host']}:{opts['port']} "
                          f"(anonymous {'allowed' if settings.DIAL_LDAP_ALLOW_ANONYMOUS else 'disabled'}) - "
                          "bind as cn=directory,dc=<event>,dc=dial with the event's directory token")
        try:
            asyncio.run(serve(opts["host"], opts["port"], ssl_context))
        except KeyboardInterrupt:
            pass
        self.stdout.write("DIAL LDAP phonebook stopped")
