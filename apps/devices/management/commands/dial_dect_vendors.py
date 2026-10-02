"""``manage.py dial_dect_vendors`` - load the built-in DECT vendor (EMC) table. Idempotent.

    manage.py dial_dect_vendors            # insert missing rows
    manage.py dial_dect_vendors --update   # also refresh names of built-in rows (never user rows)
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Load the built-in DECT manufacturer (EMC) table into DECTManufacturer."

    def add_arguments(self, parser):
        parser.add_argument("--update", action="store_true",
                            help="Refresh name/models of rows that are still source=builtin.")

    def handle(self, *args, **opts):
        from apps.devices.dect_vendors import load_builtin_manufacturers

        created, updated = load_builtin_manufacturers(overwrite_names=opts["update"])
        self.stdout.write(self.style.SUCCESS(f"DECT vendors: {created} created, {updated} updated"))
