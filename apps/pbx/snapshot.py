"""Venue-agent config sync: snapshots of one event's Asterisk realtime rows.

In ``PBXConnection.Provisioning.AGENT`` mode the venue Asterisk does not read PET's PostgreSQL. A small
agent next to it polls :func:`build_snapshot` (``GET /api/v1/pbx/snapshot/``) and writes the rows into a
local PostgreSQL with the same schema (:func:`venue_schema_sql`); Asterisk keeps using realtime against
localhost and the venue survives WAN outages with the last snapshot. Heartbeats
(:func:`apply_heartbeat`) carry the applied version and, optionally, the ``ps_contacts`` rows so
registration status in PET keeps working.

Rows are attributed to an event the same way :class:`~apps.pbx.backends.asterisk.AsteriskPBX` writes
them: endpoints by ``accountcode`` (= ``event.slug[:20]``) or ``context`` (= ``pet-<slug>``), auths/AORs by
sharing the endpoint id, IP identifies and contacts by their ``endpoint`` column, dialplan rows and
mailboxes by ``context``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from decimal import Decimal

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.pbx import dialplan as dp
from apps.pbx import pbx_connection
from apps.pbx.models import (
    Cdr,
    DialplanEntry,
    PBXConnection,
    PsAor,
    PsAuth,
    PsContact,
    PsEndpoint,
    PsEndpointIdIp,
    VoicemailUser,
)

log = logging.getLogger("pet.pbx.snapshot")

DEFAULT_POLL_INTERVAL = 15
#: tables the agent pulls (``table -> (model, key column)``); ``ps_contacts`` flows the other way (heartbeat)
SNAPSHOT_TABLES: dict[str, tuple[type, str]] = {
    "ps_endpoints": (PsEndpoint, "id"),
    "ps_auths": (PsAuth, "id"),
    "ps_aors": (PsAor, "id"),
    "ps_endpoint_id_ips": (PsEndpointIdIp, "id"),
    "extensions": (DialplanEntry, "id"),
    "voicemail_users": (VoicemailUser, "uniqueid"),
}
#: every realtime table the venue Asterisk needs (DDL), in dependency-free order
SCHEMA_MODELS = (PsEndpoint, PsAuth, PsAor, PsContact, PsEndpointIdIp, DialplanEntry, VoicemailUser, Cdr)
#: bookkeeping columns that change on every re-sync without changing what Asterisk sees: the mailbox
#: ``stamp`` and the surrogate ``id`` of dialplan rows (``_write_rows`` deletes and re-creates them)
VOLATILE_COLUMNS = {"voicemail_users": {"stamp"}, "extensions": {"id"}}
CACHE_KEY = "pbx:snapshot:{pk}:version"
CACHE_TTL = 7 * 24 * 3600


# --------------------------------------------------------------------------- scoping

def endpoint_ids(event) -> list[str]:
    return list(endpoint_queryset(event).order_by("id").values_list("id", flat=True))


def endpoint_queryset(event):
    return PsEndpoint.objects.filter(Q(accountcode=event.slug[:20]) | Q(context=dp.event_context(event)))


def event_querysets(event) -> dict[str, object]:
    """Per snapshot table: the queryset of rows belonging to ``event`` (ordered by key)."""
    ctx = dp.event_context(event)
    ids = endpoint_ids(event)
    return {
        "ps_endpoints": endpoint_queryset(event).order_by("id"),
        "ps_auths": PsAuth.objects.filter(id__in=ids).order_by("id"),
        "ps_aors": PsAor.objects.filter(id__in=ids).order_by("id"),
        "ps_endpoint_id_ips": PsEndpointIdIp.objects.filter(endpoint__in=ids).order_by("id"),
        "extensions": DialplanEntry.objects.filter(context=ctx).order_by("id"),
        "voicemail_users": VoicemailUser.objects.filter(context=ctx).order_by("uniqueid"),
    }


def contacts_queryset(event):
    return PsContact.objects.filter(endpoint__in=endpoint_ids(event))


# --------------------------------------------------------------------------- rows / version

def _plain(value):
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def row_dict(obj) -> dict:
    """Model instance -> ``{db column: plain JSON value}`` (all concrete columns)."""
    return {f.column: _plain(getattr(obj, f.attname)) for f in obj._meta.concrete_fields}


def build_tables(event) -> dict:
    out = {}
    for table, qs in event_querysets(event).items():
        key = SNAPSHOT_TABLES[table][1]
        out[table] = {"key": key, "rows": [row_dict(o) for o in qs]}
    return out


def tables_version(tables: dict) -> str:
    """sha256 over the canonical JSON of ``tables`` (sorted keys, rows in canonical order, volatile columns
    dropped) - identical content yields an identical version regardless of row order or surrogate ids."""
    canon = {}
    for table, spec in sorted(tables.items()):
        skip = VOLATILE_COLUMNS.get(table, set())
        rows = [{k: v for k, v in r.items() if k not in skip} for r in spec["rows"]]
        rows.sort(key=lambda r: json.dumps(r, sort_keys=True, default=str))
        canon[table] = {"key": spec["key"], "rows": rows}
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def poll_interval(event, conn=None) -> int:
    conn = conn if conn is not None else pbx_connection(event)
    return int(conn.agent_poll_interval or DEFAULT_POLL_INTERVAL) if conn is not None else DEFAULT_POLL_INTERVAL


def build_snapshot(event) -> dict:
    """The full payload served by ``GET /api/v1/pbx/snapshot/?event=<slug>``."""
    tables = build_tables(event)
    version = tables_version(tables)
    cache.set(CACHE_KEY.format(pk=event.pk), version, CACHE_TTL)
    return {
        "event": event.slug,
        "version": version,
        "generated_at": timezone.now().isoformat(),
        "poll_interval": poll_interval(event),
        "tables": tables,
    }


def snapshot_version(event) -> str:
    version = tables_version(build_tables(event))
    cache.set(CACHE_KEY.format(pk=event.pk), version, CACHE_TTL)
    return version


def notify_if_changed(event) -> bool:
    """Emit the ``pbx.snapshot.changed`` webhook when the event's snapshot version differs from the last
    one we cached. Only for events provisioned through a venue agent; never raises."""
    try:
        conn = pbx_connection(event)
        if conn is None or not conn.is_agent:
            return False
        from apps.core.features import enabled

        if not enabled("webhooks"):
            return False
        key = CACHE_KEY.format(pk=event.pk)
        last = cache.get(key)
        version = tables_version(build_tables(event))
        if version == last:
            return False
        cache.set(key, version, CACHE_TTL)
        from apps.events.webhooks import emit

        emit("pbx.snapshot.changed", {"event": event.slug, "version": version, "previous": last}, event=event)
        return True
    except Exception:  # noqa: BLE001 - a notification must never break outbox delivery
        log.debug("snapshot change notification failed", exc_info=True)
        return False


# --------------------------------------------------------------------------- schema (DDL)

def _pg_schema_editor():
    """A PostgreSQL schema editor that never connects - the dev/test DB may be SQLite."""
    from django.db import connection

    if connection.vendor == "postgresql":
        editor = connection.schema_editor(collect_sql=True, atomic=False)
    else:
        from django.db.backends.postgresql.base import DatabaseWrapper
        from django.db.backends.postgresql.schema import DatabaseSchemaEditor

        stub = DatabaseWrapper({"ENGINE": "django.db.backends.postgresql", "NAME": "venue", "OPTIONS": {},
                                "TIME_ZONE": None, "CONN_HEALTH_CHECKS": False, "CONN_MAX_AGE": 0,
                                "AUTOCOMMIT": True})
        editor = DatabaseSchemaEditor(stub, collect_sql=True, atomic=False)
    editor.deferred_sql = []
    return editor


def _if_not_exists(sql: str) -> str:
    for prefix in ("CREATE TABLE ", "CREATE UNIQUE INDEX ", "CREATE INDEX "):
        if sql.startswith(prefix) and not sql.startswith(prefix + "IF NOT EXISTS"):
            return prefix + "IF NOT EXISTS " + sql[len(prefix):]
    return sql


def _unique_index_sql(model, editor) -> list[str]:
    """``unique_together`` as ``CREATE UNIQUE INDEX IF NOT EXISTS`` (ADD CONSTRAINT has no IF NOT EXISTS)."""
    out = []
    q = editor.quote_name
    for names in model._meta.unique_together:
        cols = [model._meta.get_field(n).column for n in names]
        idx = f"{model._meta.db_table}_{'_'.join(cols)}_uniq"[:63]
        out.append(f"CREATE UNIQUE INDEX IF NOT EXISTS {q(idx)} ON {q(model._meta.db_table)} "
                   f"({', '.join(q(c) for c in cols)})")
    return out


def _fallback_table_sql(model) -> str:
    """Hand-derived DDL (only used if the PostgreSQL backend cannot be imported, e.g. no psycopg)."""
    types = {"CharField": "varchar({max_length})", "TextField": "text", "IntegerField": "integer",
             "PositiveIntegerField": "integer", "BigIntegerField": "bigint", "FloatField": "double precision",
             "DateTimeField": "timestamp with time zone", "BooleanField": "boolean",
             "AutoField": "integer GENERATED BY DEFAULT AS IDENTITY",
             "BigAutoField": "bigint GENERATED BY DEFAULT AS IDENTITY"}
    cols = []
    for f in model._meta.concrete_fields:
        t = types.get(f.get_internal_type(), "text").format(max_length=getattr(f, "max_length", None) or 255)
        line = f'"{f.column}" {t} {"NULL" if f.null else "NOT NULL"}'
        if f.primary_key:
            line += " PRIMARY KEY"
        cols.append(line)
    return f'CREATE TABLE IF NOT EXISTS "{model._meta.db_table}" ({", ".join(cols)})'


def venue_schema_sql() -> str:
    """``CREATE TABLE IF NOT EXISTS`` (+ indexes) for every realtime table the venue Asterisk needs."""
    lines = ["-- PET venue realtime schema (PostgreSQL) - generated from apps.pbx.models; safe to re-run.", ""]
    try:
        editor = _pg_schema_editor()
    except Exception as exc:  # noqa: BLE001 - psycopg missing on this host; derive DDL by hand
        log.warning("PostgreSQL schema editor unavailable (%s) - using hand-derived DDL", exc)
        editor = None
    for model in SCHEMA_MODELS:
        lines.append(f"-- {model._meta.db_table} ({model.__name__})")
        if editor is None:
            lines.append(_fallback_table_sql(model) + ";")
        else:
            sql, params = editor.table_sql(model)
            lines.append(_if_not_exists(sql % tuple(params) if params else sql) + ";")
            for stmt in editor._model_indexes_sql(model):
                s = str(stmt)
                if s.endswith(" varchar_pattern_ops)"):
                    continue  # Django's LIKE-helper indexes are irrelevant for Asterisk lookups
                lines.append(_if_not_exists(s) + ";")
            lines.extend(s + ";" for s in _unique_index_sql(model, editor))
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- heartbeat

def _coerce_contact(row: dict, allowed: set[str]) -> dict | None:
    fields = {f.column: f for f in PsContact._meta.concrete_fields}
    out = {}
    for col, value in dict(row).items():
        f = fields.get(col)
        if f is None:
            continue
        try:
            out[f.attname] = f.to_python(value) if value is not None else None
        except ValidationError:
            out[f.attname] = None
    if not out.get("id") or out.get("endpoint") not in allowed:
        return None
    return out


def replace_contacts(event, rows) -> int:
    """Replace PET's ``ps_contacts`` rows for the event's endpoints with what the agent reports.
    Rows for endpoints of other events are ignored. Returns the number of rows written."""
    allowed = set(endpoint_ids(event))
    contacts = []
    for row in rows or []:
        if isinstance(row, dict):
            data = _coerce_contact(row, allowed)
            if data is not None:
                contacts.append(PsContact(**data))
    with transaction.atomic():
        PsContact.objects.filter(endpoint__in=allowed).delete()
        if contacts:
            PsContact.objects.bulk_create(contacts, ignore_conflicts=True)
    return len(contacts)


def apply_heartbeat(event, payload: dict) -> PBXConnection | None:
    """Store the agent's heartbeat on the event's ``PBXConnection`` (without touching ``updated_at``, so
    the cached adapter is not rebuilt every 15 s) and replace the contacts if the agent sent them.
    Returns the refreshed connection or ``None`` when the event has no PBX connection row."""
    payload = dict(payload or {})
    if "contacts" in payload and isinstance(payload.get("contacts"), list):
        replace_contacts(event, payload["contacts"])
    conn = pbx_connection(event)
    if conn is None:
        return None
    asterisk_ok = payload.get("asterisk_ok")
    if isinstance(asterisk_ok, str):
        asterisk_ok = asterisk_ok.strip().lower() in ("1", "true", "yes", "ok")
    fields = {
        "agent_last_seen": timezone.now(),
        "agent_version": str(payload.get("version") or "")[:64],
        "agent_host": str(payload.get("hostname") or "")[:200],
        "agent_software": str(payload.get("agent_version") or "")[:64],
        "agent_asterisk_ok": asterisk_ok if isinstance(asterisk_ok, bool) else None,
        "agent_message": str(payload.get("message") or "")[:500],
    }
    PBXConnection.objects.filter(pk=conn.pk).update(**fields)
    for k, v in fields.items():
        setattr(conn, k, v)
    return conn


def agent_state(conn: PBXConnection | None, event=None) -> dict:
    """Agent status for API/CLI/portal: the ``agent_*`` fields plus stale/behind and the current version."""
    if conn is None:
        return {"enabled": False}
    event = event or conn.event
    current = snapshot_version(event)
    return {
        "enabled": conn.is_agent,
        "poll_interval": int(conn.agent_poll_interval or DEFAULT_POLL_INTERVAL),
        "last_seen": conn.agent_last_seen,
        "applied_version": conn.agent_version,
        "current_version": current,
        "host": conn.agent_host,
        "software": conn.agent_software,
        "asterisk_ok": conn.agent_asterisk_ok,
        "message": conn.agent_message,
        "stale": conn.agent_is_stale,
        "behind": conn.agent_version != current,
    }
