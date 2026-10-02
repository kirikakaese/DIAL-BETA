"""``manage.py pet_provisioning_profiles`` - create the global built-in autoprovisioning profiles
(Snom, Yealink, Grandstream, Cisco SPA). Idempotent: profiles whose name already exists are skipped unless
``--update`` is given, which rewrites them to the shipped version (e.g. after PET added new settings).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create the built-in global ProvisioningProfile rows (skips existing names unless --update)."

    def add_arguments(self, parser):
        parser.add_argument("--update", action="store_true",
                            help="Overwrite existing built-in profiles with the current shipped templates")

    def handle(self, *args, **opts):
        from apps.devices.provisioning_templates import BUILTIN_PROFILES, load_builtin_profiles

        created = load_builtin_profiles(update=opts["update"])
        rest = len(BUILTIN_PROFILES) - created
        self.stdout.write(self.style.SUCCESS(
            f"Provisioning profiles: {created} created, {rest} {'updated' if opts['update'] else 'already present'}"))
