"""``pet`` - a thin command-line client for the PET REST API.

Configuration comes from ``--url`` / ``--token`` or the environment variables ``PET_URL`` and
``PET_TOKEN`` (a service-account token minted with ``manage.py pet_token`` or in the portal).

Examples::

    export PET_URL=http://localhost:8000 PET_TOKEN=pet_...
    pet health [--event demo]
    pet events list
    pet events transition demo live
    pet events export demo > demo-backup.json
    pet extensions list --event demo --state requested
    pet extensions create --event demo --number 4242 --type sip
    pet extensions create --event demo --number 4700 --type trunk --block-digits 2
    pet extensions approve <id>
    pet queue --event demo
    pet phonebook --event demo --format csv
    pet phonebook directory --event demo [--rotate]
    pet dect rfps --event demo
    pet resync --event demo
    pet pbx outbox --event demo [--retry-dead]
    pet pbx connection show --event demo
    pet pbx connection set --event demo --pbx '{"backend":"asterisk","ari_url":"http://10.1.1.5:8088/ari", ...}'
    pet pbx connection reset --event demo [--part pbx|dect|all]
    pet pbx connection set --event demo --provisioning agent --agent-poll-interval 15
    pet pbx agent status --event demo
    pet pbx snapshot --event demo [--out snapshot.json]
    pet pages list --event demo
    pet pages show --event demo <page-slug>

Only ``requests`` (already a project dependency) is used; table output is plain text.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Sequence

import requests

API_PREFIX = "/api/v1"
DEFAULT_URL = "http://localhost:8000"


class CLIError(Exception):
    pass


# --------------------------------------------------------------------------- output helpers


def render_table(rows: Iterable[dict], columns: Sequence[str]) -> str:
    """Render ``rows`` as a fixed-width text table with the given column keys."""
    rows = list(rows)
    cells = [[_cell(r.get(c)) for c in columns] for r in rows]
    widths = [len(c) for c in columns]
    for row in cells:
        for i, v in enumerate(row):
            widths[i] = max(widths[i], len(v))
    line = "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    out = [line, "  ".join("-" * w for w in widths)]
    for row in cells:
        out.append("  ".join(v.ljust(widths[i]) for i, v in enumerate(row)))
    if not rows:
        out.append("(no results)")
    return "\n".join(out)


def _cell(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ",".join(_cell(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def render_csv(rows: Iterable[dict], columns: Sequence[str]) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in columns])
    return buf.getvalue().rstrip("\n")


def emit(args, rows, columns):
    """Print rows as table / csv / json depending on ``--format``."""
    fmt = getattr(args, "format", "table") or "table"
    if fmt == "json":
        print(json.dumps(rows, indent=2, default=str))
    elif fmt == "csv":
        print(render_csv(rows, columns))
    else:
        print(render_table(rows, columns))


# --------------------------------------------------------------------------- HTTP client


class Client:
    def __init__(self, url: str, token: str | None, timeout: float = 15.0):
        self.base = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"
        self.session.headers["User-Agent"] = "pet-cli/1.0"
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.timeout = timeout

    def request(self, method: str, path: str, *, params=None, data=None, raw=False):
        url = f"{self.base}{API_PREFIX}{path}"
        try:
            resp = self.session.request(method, url, params=params, json=data, timeout=self.timeout)
        except requests.RequestException as exc:
            raise CLIError(f"{method} {url}: {exc}") from exc
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                detail = body.get("detail") if isinstance(body, dict) else body
            except ValueError:
                detail = resp.text[:300]
            raise CLIError(f"{method} {path} -> HTTP {resp.status_code}: {detail}")
        if raw:
            return resp
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def get(self, path, **params):
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path, data=None, **params):
        return self.request("POST", path, data=data or {}, params=params or None)

    def delete(self, path):
        return self.request("DELETE", path)

    def paginate(self, path, **params) -> list[dict]:
        """Follow DRF limit/offset pagination and return all results."""
        out: list[dict] = []
        params = {k: v for k, v in params.items() if v is not None}
        params.setdefault("limit", 200)
        offset = 0
        while True:
            page = self.get(path, offset=offset, **params)
            if isinstance(page, list):  # unpaginated endpoint
                return page
            results = page.get("results", [])
            out.extend(results)
            if not page.get("next") or not results:
                return out
            offset += len(results)


# --------------------------------------------------------------------------- commands

EVENT_COLS = ["slug", "name", "state", "start_date", "end_date", "location"]
EXT_COLS = [
    "id",
    "number",
    "type",
    "state",
    "owner",
    "display_name",
    "location_hint",
    "in_phonebook",
]
DEV_COLS = ["id", "type", "state", "name", "ipei", "sip_username", "owner", "extensions"]
RFP_COLS = ["omm_id", "name", "status", "cluster", "location", "active_calls", "handsets"]
PB_COLS = ["number", "name", "type", "location", "description"]


def cmd_health(client: Client, args) -> int:
    try:
        data = client.get("/health/", event=getattr(args, "event", None))
    except CLIError as exc:
        # /health/ answers 503 when a backend is down - still show what we got
        print(str(exc))
        return 1
    print(json.dumps(data, indent=2))
    return 0 if data.get("ok") else 1


def cmd_events(client: Client, args) -> int:
    if args.action == "list":
        emit(args, client.paginate("/events/"), EVENT_COLS)
    elif args.action == "show":
        print(json.dumps(client.get(f"/events/{args.slug}/"), indent=2))
    elif args.action == "transition":
        ev = client.post(f"/events/{args.slug}/transition/", {"state": args.state})
        print(f"{ev['slug']}: state is now {ev['state']}")
    elif args.action == "schedule":
        body = {}
        if args.clear:
            body = {"registration_opens_at": None, "goes_live_at": None, "archives_at": None}
        for opt, field in (("registration", "registration_opens_at"), ("live", "goes_live_at"),
                           ("archive", "archives_at")):
            value = getattr(args, opt)
            if value is not None:
                body[field] = value or None  # --live "" clears just that one
        if not body:
            raise CLIError("give --registration/--live/--archive <ISO 8601 datetime> and/or --clear")
        ev = client.request("PATCH", f"/events/{args.slug}/", data=body)
        if getattr(args, "format", "table") == "json":
            print(json.dumps(ev, indent=2, default=str))
            return 0
        for field in ("registration_opens_at", "goes_live_at", "archives_at"):
            print(f"{field}: {ev.get(field) or '-'}")
        nxt = ev.get("next_scheduled_transition")
        print(f"next: {nxt['state']} at {nxt['at']}" if nxt else "next: -")
    elif args.action == "export":
        data = client.get(f"/events/{args.slug}/export/")
        json.dump(data, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    return 0


def cmd_extensions(client: Client, args) -> int:
    if args.action == "list":
        rows = client.paginate(
            "/extensions/",
            event__slug=args.event,
            state=args.state,
            type=args.type,
            search=args.search,
        )
        emit(args, rows, EXT_COLS)
    elif args.action == "create":
        payload = {"event": args.event, "number": args.number, "type": args.type}
        if args.display_name:
            payload["display_name"] = args.display_name
        if args.location:
            payload["location_hint"] = args.location
        if args.block_digits:
            payload["block_digits"] = args.block_digits
        ext = client.post("/extensions/", payload)
        label = ext["number"]
        if ext.get("block_range"):
            label = f"{ext['block_range'][0]}-{ext['block_range'][1]}"
        print(f"{label} ({ext['type']}) -> {ext['state']}  id={ext['id']}")
    elif args.action in ("approve", "reject"):
        ext = client.post(f"/extensions/{args.id}/{args.action}/", {"note": args.note or ""})
        print(f"{ext['number']}: {ext['state']}")
    elif args.action == "delete":
        client.delete(f"/extensions/{args.id}/")
        print(f"{args.id}: deleted")
    elif args.action == "import":
        return _extensions_import(client, args)
    return 0


IMPORT_COLS = ["line", "number", "type", "owner", "action", "messages"]


def _extensions_import(client: Client, args) -> int:
    try:
        with open(args.file, "rb") as fh:
            text = fh.read().decode("utf-8-sig")
    except OSError as exc:
        raise CLIError(f"cannot read {args.file}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise CLIError(f"{args.file} is not UTF-8 encoded: {exc}") from exc
    data = client.post("/extensions/import/", {"csv": text, "dry_run": bool(args.dry_run),
                                                "create_users": bool(args.create_users)}, event=args.event)
    plan = data.get("plan", [])
    fmt = getattr(args, "format", "table") or "table"
    if fmt == "json":
        print(json.dumps(data, indent=2, default=str))
        return 0 if not data.get("errors") else 1
    rows = [{**r, "owner": r.get("owner") or ("(orga)" if r.get("ownerless") else "-"),
             "messages": "; ".join(r.get("messages") or [])} for r in plan]
    emit(args, rows, IMPORT_COLS)
    if fmt == "table":
        if data.get("unknown_columns"):
            print(f"ignored columns: {', '.join(data['unknown_columns'])}")
        if data.get("dry_run"):
            creatable = sum(1 for r in plan if r.get("action") == "create")
            print(f"dry run: {creatable} of {len(plan)} row(s) would be imported, {len(data.get('errors', []))} "
                  f"skipped/failed")
        else:
            print(f"imported {data.get('applied', 0)} extension(s), created {data.get('users_created', 0)} "
                  f"user(s), {len(data.get('errors', []))} row(s) skipped/failed")
    return 0 if not data.get("errors") else 1


def cmd_devices(client: Client, args) -> int:
    rows = client.paginate("/devices/", event__slug=args.event, type=args.type, state=args.state)
    emit(args, rows, DEV_COLS)
    return 0


def cmd_queue(client: Client, args) -> int:
    rows = client.paginate(
        "/extensions/", event__slug=args.event, state="requested", ordering="created_at"
    )
    emit(
        args, rows, ["id", "number", "type", "owner", "display_name", "request_note", "created_at"]
    )
    return 0


def cmd_phonebook(client: Client, args) -> int:
    if getattr(args, "action", None) == "directory":
        return cmd_phonebook_directory(client, args)
    data = client.get("/phonebook/", event=args.event, q=args.search)
    emit(args, data.get("results", []), PB_COLS)
    return 0


def cmd_phonebook_directory(client: Client, args) -> int:
    """Remote-directory URLs (desk phones / OMM) and LDAP details; ``--rotate`` mints a new token first."""
    if args.rotate:
        data = client.post("/phonebook/directory/rotate/", event=args.event)
    else:
        data = client.get("/phonebook/directory/", event=args.event)
    if getattr(args, "format", "table") == "json":
        print(json.dumps(data, indent=2))
        return 0
    print(f"enabled: {_cell(data.get('enabled'))}")
    print(f"token:   {data.get('token')}")
    for vendor, url in (data.get("urls") or {}).items():
        print(f"  {vendor:<12} {url}")
    ldap = data.get("ldap") or {}
    if ldap:
        print(f"ldap: {ldap.get('host')}:{ldap.get('port')}  base {ldap.get('base_dn')}  bind {ldap.get('bind_dn')}")
    return 0


def cmd_dect(client: Client, args) -> int:
    if args.action == "rfps":
        rows = client.paginate("/dect/rfps/", event__slug=args.event)
        emit(args, rows, RFP_COLS)
    elif args.action == "handsets":
        rows = client.paginate("/dect/handsets/", event__slug=args.event)
        emit(
            args,
            rows,
            ["ipei", "state", "owner", "last_seen_rfp", "battery_percent", "rssi", "handset_model"],
        )
    elif args.action == "alerts":
        rows = client.paginate(
            "/dect/alerts/", event__slug=args.event, open="1" if args.open else None
        )
        emit(args, rows, ["id", "severity", "kind", "message", "created_at", "resolved_at"])
    elif args.action == "sync":
        print(json.dumps(client.post("/dect/sync/", event=args.event), indent=2))
    return 0


def cmd_resync(client: Client, args) -> int:
    data = client.post("/pbx/resync/", event=args.event)
    print(f"{data['event']}: {data['synced']} extensions synced to {data['backend']}")
    return 0


JOB_COLS = ["created_at", "kind", "target_type", "target_id", "state", "attempts", "next_attempt_at", "last_error"]


def cmd_pbx(client: Client, args) -> int:
    if args.action == "outbox":
        if args.retry_dead:
            data = client.post("/pbx/outbox/retry/", event=args.event)
            print(f"{data['event']}: {data['retried']} dead job(s) re-queued")
        data = client.get("/pbx/outbox/", event=args.event)
        s = data["stats"]
        if getattr(args, "format", "table") == "json":
            print(json.dumps(data, indent=2, default=str))
            return 0
        print(f"{data['event']} @ {s.get('backend')}{' (sync mode)' if s.get('sync_mode') else ''}: "
              f"{s['pending']} pending, {s['sending']} sending, {s['failed']} failed, {s['dead']} dead, "
              f"{s['delivered']} delivered")
        age = s.get("oldest_pending_age")
        print(f"oldest open job: {age}s ago" if age is not None else "oldest open job: -",
              f"| last delivery: {s.get('last_delivery_at') or '-'}")
        print()
        emit(args, data.get("jobs", []), JOB_COLS)
    elif args.action == "connection":
        if args.what == "show":
            data = client.get("/pbx/connection/", event=args.event)
        elif args.what == "set":
            body = {}
            for part in ("pbx", "dect"):
                raw = getattr(args, part)
                if raw:
                    try:
                        body[part] = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise CLIError(f"--{part} must be a JSON object: {exc}") from exc
            if getattr(args, "provisioning", None):
                body.setdefault("pbx", {})["provisioning"] = args.provisioning
            if getattr(args, "agent_poll_interval", None):
                body.setdefault("pbx", {})["agent_poll_interval"] = args.agent_poll_interval
            if not body:
                raise CLIError("give --pbx and/or --dect as JSON objects (or --provisioning / --agent-poll-interval)")
            data = client.request("PATCH", "/pbx/connection/", params={"event": args.event}, data=body)
        else:
            data = client.request("DELETE", "/pbx/connection/", params={"event": args.event, "part": args.part})
        if getattr(args, "format", "table") == "json":
            print(json.dumps(data, indent=2, default=str))
            return 0
        for part in ("pbx", "dect"):
            conn = data.get(part)
            eff = data["effective"][part]
            if conn is None:
                print(f"{part}: server default ({eff})")
            else:
                where = conn.get("ari_url") or conn.get("ami_host") or conn.get("host") or "-"
                mode = f", provisioning {conn['provisioning']}" if conn.get("provisioning") else ""
                print(f"{part}: {conn['backend_label']} @ {where} (adapter {eff}{mode})")
    elif args.action == "agent":
        data = client.get("/pbx/connection/", event=args.event)
        conn = data.get("pbx")
        if getattr(args, "format", "table") == "json":
            print(json.dumps(conn, indent=2, default=str))
            return 0
        if conn is None:
            print(f"{args.event}: no PBX connection (server default)")
            return 0
        state = "stale" if conn.get("agent_is_stale") else ("behind" if conn.get("agent_behind") else "ok")
        print(f"{args.event}: provisioning {conn.get('provisioning')} - venue agent {state}")
        for label, key in (("last seen", "agent_last_seen"), ("host", "agent_host"), ("software", "agent_software"),
                           ("applied version", "agent_version"), ("current version", "snapshot_version"),
                           ("asterisk ok", "agent_asterisk_ok"), ("message", "agent_message"),
                           ("poll interval", "agent_poll_interval")):
            print(f"  {label:16} {_cell(conn.get(key))}")
    elif args.action == "snapshot":
        data = client.get("/pbx/snapshot/", event=args.event)
        text = json.dumps(data, indent=2, default=str, sort_keys=True)
        if args.out and args.out != "-":
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            rows = sum(len(t.get("rows", [])) for t in data.get("tables", {}).values())
            print(f"{data.get('event')}: snapshot {data.get('version', '')[:12]} ({rows} rows) written to {args.out}")
        else:
            print(text)
    return 0


def cmd_me(client: Client, args) -> int:
    print(json.dumps(client.get("/me/"), indent=2))
    return 0


PAGE_COLS = ["id", "slug", "title", "order", "published", "show_on_dashboard", "updated_at"]


def cmd_pages(client: Client, args) -> int:
    if args.action == "list":
        emit(args, client.paginate("/pages/", event=args.event), PAGE_COLS)
    elif args.action == "show":
        rows = [p for p in client.paginate("/pages/", event=args.event) if p.get("slug") == args.slug]
        if not rows:
            raise CLIError(f"no page '{args.slug}' in event {args.event}")
        page = rows[0]
        if getattr(args, "format", "table") == "json":
            print(json.dumps(page, indent=2, default=str))
            return 0
        print(f"# {page['title']}")
        print(f"({page['slug']}, order {page['order']}, {'published' if page['published'] else 'unpublished'}, "
              f"updated {page['updated_at']})")
        print()
        print(page.get("body") or "")
    return 0


# --------------------------------------------------------------------------- argument parsing


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pet", description="Command-line client for the PET REST API.")
    p.add_argument(
        "--url", default=None, help="Base URL of PET (default: $PET_URL or http://localhost:8000)"
    )
    p.add_argument("--token", default=None, help="Service token pet_... (default: $PET_TOKEN)")
    p.add_argument(
        "--format", choices=["table", "csv", "json"], default="table", help="Output format"
    )
    p.add_argument("--timeout", type=float, default=15.0)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="Health of PET, PBX and DECT backends").set_defaults(
        func=cmd_health
    )
    sub.choices["health"].add_argument("--event", help="check this event's venue PBX/DECT, not the server default")
    sub.add_parser("me", help="Show the authenticated user / service account").set_defaults(
        func=cmd_me
    )

    ev = sub.add_parser("events", help="Manage events")
    evs = ev.add_subparsers(dest="action", required=True)
    evs.add_parser("list")
    evs.add_parser("show").add_argument("slug")
    tr = evs.add_parser("transition")
    tr.add_argument("slug")
    tr.add_argument("state", choices=["draft", "registration", "live", "archived"])
    sc = evs.add_parser("schedule", help="Schedule automatic state changes (PATCH /events/<slug>/)")
    sc.add_argument("slug")
    sc.add_argument("--registration", metavar="ISO", help="open registration at this ISO 8601 datetime")
    sc.add_argument("--live", metavar="ISO", help="go live at this ISO 8601 datetime")
    sc.add_argument("--archive", metavar="ISO", help="archive at this ISO 8601 datetime")
    sc.add_argument("--clear", action="store_true", help="remove all scheduled transitions")
    evs.add_parser("export").add_argument("slug")
    ev.set_defaults(func=cmd_events)

    ex = sub.add_parser("extensions", help="List / create / moderate extensions")
    exs = ex.add_subparsers(dest="action", required=True)
    ls = exs.add_parser("list")
    ls.add_argument("--event", required=True)
    ls.add_argument("--state", default=None)
    ls.add_argument("--type", default=None)
    ls.add_argument("--search", default=None)
    cr = exs.add_parser("create")
    cr.add_argument("--event", required=True)
    cr.add_argument("--number", required=True)
    cr.add_argument("--type", default="dect")
    cr.add_argument("--display-name", default=None)
    cr.add_argument("--location", default=None)
    cr.add_argument("--block-digits", type=int, choices=(1, 2, 3), default=None,
                    help="SIP trunks: trailing wildcard digits of the number block (2 = 100 numbers, e.g. 4700-4799)")
    for name in ("approve", "reject"):
        m = exs.add_parser(name)
        m.add_argument("id")
        m.add_argument("--note", default="")
    exs.add_parser("delete").add_argument("id")
    im = exs.add_parser("import", help="Bulk import extensions/users from a CSV file (POST /extensions/import/)")
    im.add_argument("--event", required=True)
    im.add_argument("--file", required=True, help="CSV file (header: number,type,display_name,email,...)")
    im.add_argument("--dry-run", action="store_true", help="Only show what would happen")
    im.add_argument("--create-users", action="store_true", help="Create accounts for unknown e-mail addresses")
    ex.set_defaults(func=cmd_extensions)

    dv = sub.add_parser("devices", help="List devices")
    dvs = dv.add_subparsers(dest="action", required=True)
    dl = dvs.add_parser("list")
    dl.add_argument("--event", required=True)
    dl.add_argument("--type", default=None)
    dl.add_argument("--state", default=None)
    dv.set_defaults(func=cmd_devices)

    q = sub.add_parser("queue", help="Extensions awaiting approval")
    q.add_argument("--event", required=True)
    q.set_defaults(func=cmd_queue)

    pb = sub.add_parser("phonebook", help="Public phonebook of an event")
    pb.add_argument("action", nargs="?", choices=["directory"], default=None,
                    help="'directory': remote-phonebook URLs for desk phones / OMM + LDAP details")
    pb.add_argument("--event", required=True)
    pb.add_argument("--search", default=None)
    pb.add_argument("--rotate", action="store_true", help="with 'directory': mint a new directory token")
    pb.set_defaults(func=cmd_phonebook)

    de = sub.add_parser("dect", help="DECT infrastructure")
    des = de.add_subparsers(dest="action", required=True)
    for name in ("rfps", "handsets", "sync"):
        des.add_parser(name).add_argument("--event", required=True)
    al = des.add_parser("alerts")
    al.add_argument("--event", required=True)
    al.add_argument("--open", action="store_true", help="Only unresolved alerts")
    de.set_defaults(func=cmd_dect)

    rs = sub.add_parser("resync", help="Push the whole event to the PBX")
    rs.add_argument("--event", required=True)
    rs.set_defaults(func=cmd_resync)

    px = sub.add_parser("pbx", help="PBX outbox / provisioning queue")
    pxs = px.add_subparsers(dest="action", required=True)
    ob = pxs.add_parser("outbox", help="Queue stats and the most recent PBX jobs")
    ob.add_argument("--event", required=True)
    ob.add_argument("--retry-dead", action="store_true", help="Re-queue dead jobs before printing stats")
    cn = pxs.add_parser("connection", help="The event's venue PBX/DECT connection (what /e/<slug>/pbx/ edits)")
    cns = cn.add_subparsers(dest="what", required=True)
    for name in ("show", "set", "reset"):
        sp = cns.add_parser(name)
        sp.add_argument("--event", required=True)
        if name == "set":
            sp.add_argument("--pbx", help='JSON object, e.g. {"backend":"asterisk","ari_url":"http://..."}')
            sp.add_argument("--dect", help='JSON object, e.g. {"backend":"omm","host":"10.1.1.9"}')
            sp.add_argument("--provisioning", choices=["shared_db", "agent"],
                            help="how realtime rows reach the venue: shared database or venue agent (snapshots)")
            sp.add_argument("--agent-poll-interval", type=int, metavar="SECONDS",
                            help="snapshot poll interval of the venue agent")
        if name == "reset":
            sp.add_argument("--part", choices=["pbx", "dect", "all"], default="all")
    ag = pxs.add_parser("agent", help="Venue agent status (heartbeat, applied vs. current snapshot version)")
    ags = ag.add_subparsers(dest="what", required=True)
    ags.add_parser("status").add_argument("--event", required=True)
    sn = pxs.add_parser("snapshot", help="Dump the venue-agent snapshot (GET /pbx/snapshot/)")
    sn.add_argument("--event", required=True)
    sn.add_argument("--out", default="-", help="write JSON to this file instead of stdout")
    px.set_defaults(func=cmd_pbx)

    pg = sub.add_parser("pages", help="Info pages of an event (orga-written Markdown)")
    pgs = pg.add_subparsers(dest="action", required=True)
    pgs.add_parser("list").add_argument("--event", required=True)
    ps = pgs.add_parser("show", help="Print one page (Markdown body)")
    ps.add_argument("--event", required=True)
    ps.add_argument("slug")
    pg.set_defaults(func=cmd_pages)
    return p


def make_client(args) -> Client:
    url = args.url or os.environ.get("PET_URL") or DEFAULT_URL
    token = args.token or os.environ.get("PET_TOKEN")
    return Client(url, token, timeout=args.timeout)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = make_client(args)
    try:
        return args.func(client, args) or 0
    except CLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
