"""``manage.py pbx_venue_schema [--out file.sql]`` - PostgreSQL DDL for the venue agent's local database.

Prints the same ``CREATE TABLE IF NOT EXISTS`` statements as ``GET /api/v1/pbx/snapshot/schema/``.
"""
from django.core.management.base import BaseCommand

from apps.pbx.snapshot import venue_schema_sql


class Command(BaseCommand):
    help = "Print the PostgreSQL schema (realtime tables) a venue agent needs next to its Asterisk."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="-", help="output file (default: stdout)")

    def handle(self, *args, **opts):
        sql = venue_schema_sql()
        if opts["out"] in ("-", ""):
            self.stdout.write(sql)
        else:
            with open(opts["out"], "w", encoding="utf-8") as fh:
                fh.write(sql)
            self.stderr.write(f"wrote {len(sql)} bytes to {opts['out']}")
