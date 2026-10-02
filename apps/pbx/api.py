"""PBX ↔ DIAL API: hook endpoints called by Asterisk, the routing lookup and orga tools.

Hook/route endpoints authenticate with the shared secret header ``X-DIAL-PBX-Secret``
(``settings.DIAL_PBX_HOOK_SECRET``, falling back to ``ASTERISK["ARI_PASSWORD"]``).
They dispatch lazily into other apps' ``services`` modules and degrade gracefully
when those are missing (packages are built in parallel) or a feature is off.
"""
from __future__ import annotations

import hmac
import logging
from dataclasses import asdict
from importlib import import_module

from django.conf import settings
from django.http import HttpResponse
from django.urls import path
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.api.permissions import HasScope
from apps.events.models import Event
from apps.extensions.models import ENDPOINT_TYPES, Extension, ExtensionType
from apps.pbx import dialplan as dp
from apps.pbx import get_pbx, pbx_connection
from apps.pbx.base import PBXError

log = logging.getLogger("dial.pbx.api")

HOOK_KINDS = ("feature-code", "extension-idle", "cdr", "voicemail", "site-survey", "dect-claim",
              "announcement-record-start", "announcement-recorded")
FEATURE_CODE_MODULES = ("apps.callback.services", "apps.callgroups.services", "apps.extensions.feature_codes")
CDR_KEYS = ("src", "dst", "start", "answer", "end", "duration", "billsec", "disposition", "channel",
            "dstchannel", "uniqueid", "rfp", "linkedid", "dcontext", "accountcode")


# --------------------------------------------------------------------------- helpers

def hook_secret(event=None) -> str:
    """Secret the venue Asterisk must present: the event's ``PBXConnection.hook_secret`` if set, else the
    server-wide ``DIAL_PBX_HOOK_SECRET`` (or the default ARI password)."""
    if event is not None:
        conn = pbx_connection(event)
        if conn is not None and conn.hook_secret:
            return conn.hook_secret
    return getattr(settings, "DIAL_PBX_HOOK_SECRET", None) or settings.ASTERISK.get("ARI_PASSWORD") or ""


def _authorized(request, event=None) -> bool:
    secret = hook_secret(event)
    given = request.headers.get("X-DIAL-PBX-Secret", "")
    return bool(secret) and hmac.compare_digest(str(given), str(secret))


def call_service(module: str, func: str, *args, default=None, **kwargs):
    """Lazily import ``module.func`` and call it; missing/broken services degrade to ``default``."""
    try:
        mod = import_module(module)
        fn = getattr(mod, func)
    except (ImportError, AttributeError) as exc:
        log.warning("PBX hook: %s.%s not available (%s)", module, func, exc)
        return default
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - the PBX is waiting on this HTTP call; never 500
        log.exception("PBX hook: %s.%s raised", module, func)
        return default


def _flat(data) -> dict:
    """QueryDict (form-encoded from Asterisk CURL()) or dict (JSON) -> plain dict of scalars."""
    if hasattr(data, "getlist"):
        return {k: data.get(k) for k in data.keys()}
    return dict(data or {})


def _event_from(slug: str | None):
    return Event.objects.filter(slug=slug).first() if slug else None


def _unauthorized():
    return Response({"detail": "invalid PBX secret"}, status=status.HTTP_401_UNAUTHORIZED)


# --------------------------------------------------------------------------- hooks

@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([])
def hook(request, kind: str):
    data = _flat(request.data)
    event = _event_from(data.get("event"))
    if not _authorized(request, event):
        return _unauthorized()
    if kind not in HOOK_KINDS:
        return Response({"detail": f"unknown hook kind {kind!r}"}, status=status.HTTP_404_NOT_FOUND)
    if event is None:
        return Response({"handled": False, "detail": "unknown or missing event"},
                        status=status.HTTP_400_BAD_REQUEST)

    out: dict = {"handled": False}
    if kind == "feature-code":
        caller, code, target = data.get("caller", ""), data.get("code", ""), data.get("target", "") or ""
        handled = False
        for mod in FEATURE_CODE_MODULES:
            res = call_service(mod, "handle_feature_code", event, caller, code, target, default=False)
            if res:
                handled = True
                break
        out["handled"] = bool(handled)
    elif kind == "extension-idle":
        number = data.get("number", "")
        res = call_service("apps.callback.services", "on_extension_idle", event, number)
        out["handled"] = res is not None
        out["result"] = res if isinstance(res, (int, str, bool, list, dict)) else None
    elif kind == "cdr":
        record = {k: data.get(k) for k in CDR_KEYS if k in data}
        res = call_service("apps.stats.services", "ingest_cdr", event, record)
        out["handled"] = res is not None
    elif kind == "voicemail":
        duration = data.get("duration") or 0
        try:
            duration = int(float(duration))
        except (TypeError, ValueError):
            duration = 0
        res = call_service("apps.voicemail.services", "store_message", event,
                           data.get("mailbox") or data.get("mailbox_number") or "",
                           data.get("caller", ""), data.get("file_path") or data.get("file") or "", duration)
        out["handled"] = res is not None
    elif kind == "site-survey":
        rfp = call_service("apps.dect.services", "log_site_survey", event, data.get("caller", ""))
        name = getattr(rfp, "name", rfp) if rfp is not None else None
        out.update({"handled": name is not None, "rfp": str(name) if name else "",
                    "say": str(name) if name else ""})
    elif kind == "dect-claim":
        # caller = PJSIP endpoint name (device.sip_username); callerid = CALLERID(num) as a fallback
        res = call_service("apps.dect.claim", "claim_handset", event, data.get("caller", ""),
                           data.get("code", ""), callerid=data.get("callerid", ""))
        ok = bool(getattr(res, "ok", False))
        out.update({"handled": ok, "result": getattr(res, "code", "error"),
                    "number": getattr(res, "number", "") or "", "say": getattr(res, "number", "") or "",
                    "detail": getattr(res, "message", "") or ""})
    elif kind == "announcement-record-start":
        # caller = PJSIP endpoint name (device.sip_username); callerid = CALLERID(num) (the calling extension)
        res = call_service("apps.ivr.services", "begin_phone_recording", event, data.get("caller", ""),
                           data.get("code", ""), callerid=data.get("callerid", ""))
        if isinstance(res, dict) and res.get("handled"):
            out.update(res)
    elif kind == "announcement-recorded":
        duration = data.get("duration") or 0
        try:
            duration = int(float(duration))
        except (TypeError, ValueError):
            duration = 0
        res = call_service("apps.ivr.services", "finish_phone_recording", event, data.get("code", ""),
                           data.get("file") or data.get("file_path") or "", duration)
        out.update({"handled": res is not None, "number": getattr(getattr(res, "extension", None), "number", "")})
    return Response(out)


# --------------------------------------------------------------------------- route

def _device_targets(ext) -> list[str]:
    return [ds for _b, ds in dp.dial_targets(ext)]


def _targets_for_numbers(event, numbers) -> list[str]:
    """Numbers of group members -> device dial strings (falls back to a Local channel)."""
    out: list[str] = []
    for n in numbers:
        n = str(n).strip()
        if not n:
            continue
        member = Extension.objects.filter(event=event, number=n).active().first()
        if member is not None and member.is_endpoint:
            out += _device_targets(member) or [f"Local/{n}@{dp.event_context(event)}"]
        else:
            out.append(f"Local/{n}@{dp.event_context(event)}")
    return out


def _ivr_payload(ext) -> dict:
    cfg = ext.config or {}
    payload = call_service("apps.ivr.services", "dialplan_for", ext)
    if not isinstance(payload, dict) or not payload:
        payload = {
            "greeting": cfg.get("audio") or cfg.get("greeting") or "",
            "options": cfg.get("options") or {},
            "timeout": cfg.get("timeout", 5),
            "invalid": cfg.get("invalid") or "pbx-invalid",
        }
    payload.setdefault("options", {})
    if ext.type == ExtensionType.ANNOUNCEMENT:
        payload["options"] = {}
    return payload


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([])
def route(request):
    event = _event_from(request.query_params.get("event"))
    if not _authorized(request, event):
        return _unauthorized()
    number = (request.query_params.get("number") or "").strip()
    if event is None or not number:
        return Response({"detail": "event and number are required"}, status=status.HTTP_400_BAD_REQUEST)
    ctx = dp.event_context(event)
    out = {
        "event": event.slug, "number": number, "context": ctx, "type": None, "targets": [],
        "dial_string": "", "strategy": "parallel", "timeout": 30, "ivr": {}, "forward": {},
        "forward_busy": "", "forward_noanswer": "", "forward_unconditional": "",
        "priority": 0, "allow_callback": False, "ivr_greeting": "", "ivr_timeout": 5, "ivr_options": {},
    }
    ext = Extension.objects.filter(event=event, number=number).active().select_related("owner").first()
    if ext is None:
        target = call_service("apps.emergency.services", "route", event, number)
        if target:
            out.update({"type": "emergency", "targets": [str(target)], "priority": 100})
        else:
            trunk = call_service("apps.extensions.services", "trunk_for_number", event, number)
            if trunk is not None:
                # a number inside a trunk block: hand it to the remote PBX (works without realtime pattern rows)
                ds = dp.trunk_dial_string(trunk, number)
                out.update({"type": ExtensionType.TRUNK, "targets": [ds] if ds else [],
                            "timeout": trunk.ring_timeout or 30, "priority": trunk.priority,
                            "trunk": {"base": trunk.number, "range": list(trunk.block_range())}})
            else:
                target = call_service("apps.federation.services", "route", event, number)
                if target:
                    out.update({"type": "federation", "targets": [str(target)]})
        out["dial_string"] = "&".join(out["targets"])
        return Response(out)

    out.update({
        "type": ext.type, "strategy": ext.ring_strategy, "timeout": ext.ring_timeout or 30,
        "priority": ext.priority, "allow_callback": ext.allow_callback,
        "forward": {"busy": ext.forward_busy, "noanswer": ext.forward_noanswer,
                    "unconditional": ext.forward_unconditional},
        "forward_busy": ext.forward_busy, "forward_noanswer": ext.forward_noanswer,
        "forward_unconditional": ext.forward_unconditional,
    })
    cfg = ext.config or {}
    if ext.type in ENDPOINT_TYPES:
        out["targets"] = _device_targets(ext)
    elif ext.type == ExtensionType.GROUP:
        res = call_service("apps.callgroups.services", "dial_targets", ext, default=None)
        strategy = cfg.get("strategy") or ext.ring_strategy
        numbers: list = []
        if isinstance(res, dict):
            numbers = res.get("targets") or res.get("numbers") or []
            strategy = res.get("strategy") or strategy
        elif isinstance(res, tuple) and len(res) == 2:
            numbers, strategy = list(res[0]), res[1] or strategy
        elif isinstance(res, (list, set)):
            numbers = list(res)
        if not numbers:
            numbers = cfg.get("members") or []
        out["targets"] = _targets_for_numbers(event, numbers)
        out["strategy"] = "serial" if str(strategy) in ("serial", "linear", "roundrobin") else "parallel"
        # Optional extras consumed by [dial-group]: shortcode caller-ID prefix + per-member ring delays.
        out["callerid_prefix"] = call_service("apps.callgroups.services", "callerid_prefix", ext, default="") or ""
        waves = call_service("apps.callgroups.services", "dial_waves", ext, default=None) or []
        out["waves"] = [{"delay": int(w["delay"]), "targets": _targets_for_numbers(event, w["targets"])}
                        for w in waves]
        legs = [t for w in waves if not w["delay"] for t in _targets_for_numbers(event, w["targets"])]
        legs += [f"Local/{int(w['delay']):03d}*{n}@dial-group" for w in waves if w["delay"] for n in w["targets"]]
        out["dial_string_waves"] = "&".join(legs)
    elif ext.type in (ExtensionType.IVR, ExtensionType.ANNOUNCEMENT):
        ivr = _ivr_payload(ext)
        out["ivr"] = ivr
        out["ivr_greeting"] = str(ivr.get("greeting") or "")
        out["ivr_timeout"] = int(ivr.get("timeout") or 5)
        out["ivr_options"] = {str(k): str(v) for k, v in (ivr.get("options") or {}).items()}
    elif ext.type == ExtensionType.FEDERATION:
        target = call_service("apps.federation.services", "route", event, cfg.get("destination") or number)
        out["targets"] = [str(target)] if target else []
    elif ext.type == ExtensionType.BREAKOUT:
        dest = cfg.get("destination") or request.query_params.get("destination") or number
        ok, reason = call_service("apps.breakout.services", "authorize", event, ext, dest,
                                  default=(False, "breakout unavailable"))
        out["breakout"] = {"allowed": bool(ok), "reason": reason, "destination": dest}
        if ok and cfg.get("trunk"):
            out["targets"] = [f"PJSIP/{dest}@{cfg['trunk']}"]
    elif ext.type == ExtensionType.CONFERENCE:
        out["conference"] = {"name": dp.conference_name(ext), "pin": str(cfg.get("pin") or "")}
    elif ext.type == ExtensionType.TRUNK:
        ds = dp.trunk_dial_string(ext, number)
        out["targets"] = [ds] if ds else []
        out["trunk"] = {"base": ext.number, "range": list(ext.block_range())}
    out["dial_string"] = "&".join(out["targets"])
    return Response(out)


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([])
def dialplan_export(request):
    """Static dialplan for Asterisk ``#exec`` includes.

    ``?shell=1`` returns only the ``[dial-<slug>]`` shell contexts (``switch => Realtime/@``) for all
    non-archived events; without it the full static equivalent (``render_dialplan``) of one event
    (``?event=<slug>``) or all events is returned. Content-Type is text/plain.
    """
    slug = request.query_params.get("event")
    if not _authorized(request, _event_from(slug)):
        return _unauthorized()
    qs = Event.objects.filter(slug=slug) if slug else Event.objects.exclude(state=Event.State.ARCHIVED)
    if request.query_params.get("shell"):
        body = "\n".join(dp.shell_context(ev) for ev in qs.order_by("slug"))
    else:
        body = "\n".join(get_pbx(ev).render_dialplan(ev) for ev in qs.order_by("slug"))
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


# --------------------------------------------------------------------------- orga tools

def _orga_event_or_response(request):
    event = _event_from(request.query_params.get("event") or _flat(request.data).get("event"))
    if event is None:
        return None, Response({"detail": "unknown event"}, status=status.HTTP_404_NOT_FOUND)
    acct = getattr(request, "service_account", None)
    if acct is not None and acct.event_id and acct.event_id != event.pk:
        return None, Response({"detail": "token not valid for this event"}, status=status.HTTP_403_FORBIDDEN)
    if not request.user.is_orga(event):
        return None, Response({"detail": "orga role required"}, status=status.HTTP_403_FORBIDDEN)
    return event, None


@api_view(["POST"])
@permission_classes([IsAuthenticated, HasScope])
def resync(request):
    event, err = _orga_event_or_response(request)
    if err:
        return err
    try:
        n = get_pbx(event).sync_event(event)
    except PBXError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
    from apps.core.audit import log as audit

    audit(action="provision", actor=request.user, target=event, event=event, request=request,
          message=f"PBX resync: {n} extensions")
    return Response({"event": event.slug, "synced": n, "backend": get_pbx(event).name})


resync.cls.required_scopes = {"default": ["pbx:write"]}


@api_view(["GET"])
@permission_classes([IsAuthenticated, HasScope])
def pbx_status(request):
    event, err = _orga_event_or_response(request)
    if err:
        return err
    pbx = get_pbx(event)
    out = {"event": event.slug, "backend": pbx.name, "health": pbx.health(), "channels": [], "error": None}
    try:
        out["channels"] = [asdict(c) for c in pbx.active_channels(event)]
    except PBXError as exc:
        out["error"] = str(exc)
    return Response(out)


pbx_status.cls.required_scopes = {"get": ["pbx:read"], "default": ["pbx:read"]}


@api_view(["GET"])
@permission_classes([IsAuthenticated, HasScope])
def outbox_status(request):
    """PBX outbox health for one event: counts per state + the 20 most recent jobs."""
    from apps.pbx import outbox

    event, err = _orga_event_or_response(request)
    if err:
        return err
    return Response({"event": event.slug, "stats": outbox.queue_stats(event), "jobs": outbox.recent_jobs(event)})


outbox_status.cls.required_scopes = {"get": ["pbx:read"], "default": ["pbx:read"]}


@api_view(["POST"])
@permission_classes([IsAuthenticated, HasScope])
def outbox_retry(request):
    """Re-queue all dead PBX jobs of the event."""
    from apps.pbx import outbox

    event, err = _orga_event_or_response(request)
    if err:
        return err
    n = outbox.retry_dead(event)
    if n:
        from apps.core.audit import log as audit

        audit(action="provision", actor=request.user, target=event, event=event, request=request,
              message=f"PBX outbox: {n} dead jobs retried")
    return Response({"event": event.slug, "retried": n, "stats": outbox.queue_stats(event)})


outbox_retry.cls.required_scopes = {"default": ["pbx:write"]}


# --------------------------------------------------------------------------- venue connection

_SECRET_FIELDS = {"ari_password", "ami_password", "hook_secret", "password"}
_AGENT_FIELDS = ("agent_last_seen", "agent_version", "agent_host", "agent_software", "agent_asterisk_ok",
                 "agent_message")


def _connection_payload(conn, fields: list[str], readonly: tuple[str, ...] = ()) -> dict | None:
    """Serialise a connection; secrets are reported as booleans (``has_<field>``), never returned.
    ``readonly`` fields are included in the output but ignored by ``_save_connection``."""
    if conn is None:
        return None
    out = {"backend": conn.backend, "backend_label": conn.backend_label,
           "updated_at": conn.updated_at, "notes": conn.notes}
    for f in list(fields) + list(readonly):
        if f in _SECRET_FIELDS:
            out[f"has_{f}"] = bool(getattr(conn, f))
        elif f not in out:
            out[f] = getattr(conn, f)
    return out


def _connection_state(event) -> dict:
    from apps.dect import get_dect
    from apps.dect.models import DECTConnection
    from apps.pbx.forms import DECTConnectionForm, PBXConnectionForm
    from apps.pbx.models import PBXConnection
    from apps.pbx.snapshot import agent_state

    pbx_conn = PBXConnection.objects.filter(event=event).first()
    dect_conn = DECTConnection.objects.filter(event=event).first()
    pbx = _connection_payload(pbx_conn, PBXConnectionForm.Meta.fields, readonly=_AGENT_FIELDS)
    if pbx is not None:
        state = agent_state(pbx_conn, event)
        pbx.update({"agent_is_stale": state["stale"], "agent_behind": state["behind"],
                    "snapshot_version": state["current_version"]})
    return {
        "event": event.slug,
        "pbx": pbx,
        "dect": _connection_payload(dect_conn, DECTConnectionForm.Meta.fields),
        "effective": {"pbx": get_pbx(event).name, "dect": get_dect(event).name},
        "server_default": {"pbx": pbx_conn is None, "dect": dect_conn is None},
        "backends": {"pbx": dict(getattr(settings, "DIAL_PBX_BACKENDS", {})),
                     "dect": dict(getattr(settings, "DIAL_DECT_BACKENDS", {}))},
    }


def _save_connection(request, event, part: str, payload: dict):
    """Merge ``payload`` into the event's PBX/DECT connection through the orga forms (same validation,
    same keep-stored-secret semantics as the portal page). Returns a list of errors or ``[]``."""
    from django.forms.models import model_to_dict

    from apps.core.audit import log as audit
    from apps.dect import reset_dect_cache
    from apps.dect.models import DECTConnection
    from apps.pbx import reset_pbx_cache
    from apps.pbx.forms import DECTConnectionForm, PBXConnectionForm
    from apps.pbx.models import PBXConnection

    model, form_cls, reset = ((PBXConnection, PBXConnectionForm, reset_pbx_cache) if part == "pbx"
                              else (DECTConnection, DECTConnectionForm, reset_dect_cache))
    existing = model.objects.filter(event=event).first()
    instance = existing or model(event=event)
    data = {k: v for k, v in model_to_dict(instance, fields=form_cls.Meta.fields).items() if v is not None}
    data.update({k: v for k, v in dict(payload).items() if k in form_cls.Meta.fields})
    form = form_cls(data, instance=instance)
    if not form.is_valid():
        return form.errors.get_json_data()
    conn = form.save()
    reset()
    detail = f"{conn.backend}/{conn.provisioning}" if part == "pbx" else conn.backend
    audit(action="update" if existing else "create", actor=request.user, target=conn, event=event,
          request=request, message=f"{part.upper()} connection via API: {detail}")
    return []


@api_view(["GET", "PUT", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated, HasScope])
def connection(request):
    """The event's venue infrastructure (what the orga page ``/e/<slug>/pbx/`` edits).

    ``GET`` returns both connections (secrets only as ``has_*`` flags), the effective adapters and the
    backend catalogue. ``PUT``/``PATCH`` take ``{"pbx": {...}, "dect": {...}}`` (either part optional;
    omitted or empty secrets keep the stored value). ``DELETE`` removes ``?part=pbx|dect|all`` (default
    ``all``) so the event falls back to the server default.
    """
    event, err = _orga_event_or_response(request)
    if err:
        return err
    if request.method in ("PUT", "PATCH"):
        body = request.data if isinstance(request.data, dict) else _flat(request.data)
        parts = {p: body.get(p) for p in ("pbx", "dect") if isinstance(body.get(p), dict)}
        if not parts:
            return Response({"detail": "body must contain 'pbx' and/or 'dect' objects"},
                            status=status.HTTP_400_BAD_REQUEST)
        errors = {p: e for p, payload in parts.items() if (e := _save_connection(request, event, p, payload))}
        if errors:
            return Response({"detail": "validation failed", "errors": errors}, status=status.HTTP_400_BAD_REQUEST)
    elif request.method == "DELETE":
        from apps.core.audit import log as audit
        from apps.dect import reset_dect_cache
        from apps.dect.models import DECTConnection
        from apps.pbx import reset_pbx_cache
        from apps.pbx.models import PBXConnection

        part = request.query_params.get("part", "all")
        if part not in ("pbx", "dect", "all"):
            return Response({"detail": "part must be pbx, dect or all"}, status=status.HTTP_400_BAD_REQUEST)
        for name, model, reset in (("pbx", PBXConnection, reset_pbx_cache),
                                   ("dect", DECTConnection, reset_dect_cache)):
            if part in (name, "all"):
                conn = model.objects.filter(event=event).first()
                if conn is not None:
                    audit(action="delete", actor=request.user, target=conn, event=event, request=request,
                          message=f"{name.upper()} connection removed via API (server default)")
                    conn.delete()
                    reset()
    return Response(_connection_state(event))


connection.cls.required_scopes = {"get": ["pbx:read"], "default": ["pbx:write"]}


# --------------------------------------------------------------------------- venue agent (snapshot sync)

def _sync_authorized(request, event) -> bool:
    """The venue agent presents the event's hook secret (``X-DIAL-PBX-Secret``); alternatively an orga of
    the event may use a session or a service token carrying the ``pbx:sync`` scope."""
    if event is None:
        return False
    if _authorized(request, event):
        return True
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    acct = getattr(request, "service_account", None)
    if acct is not None:
        if acct.event_id and acct.event_id != event.pk:
            return False
        if not acct.has_scope("pbx:sync"):
            return False
    return user.is_orga(event)


def _etag_matches(request, version: str) -> bool:
    given = request.headers.get("If-None-Match", "")
    tags = {t.strip().removeprefix("W/").strip('"') for t in given.split(",") if t.strip()}
    return version in tags or "*" in tags


def _sync_event_or_response(request, slug):
    """``(event, None)`` when the caller may sync that event, else ``(None, error response)``."""
    event = _event_from(slug)
    if event is None:
        if _authorized(request, None) or getattr(request.user, "is_authenticated", False):
            return None, Response({"detail": "unknown event"}, status=status.HTTP_404_NOT_FOUND)
        return None, _unauthorized()
    if not _sync_authorized(request, event):
        return None, _unauthorized()
    return event, None


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([])
def snapshot(request):
    """All realtime rows of one event for the venue agent (``?event=<slug>``); ``ETag``/``If-None-Match``
    yield ``304`` while nothing changed."""
    from apps.pbx import snapshot as snap

    event, err = _sync_event_or_response(request, request.query_params.get("event"))
    if err:
        return err
    data = snap.build_snapshot(event)
    etag = f'"{data["version"]}"'
    if _etag_matches(request, data["version"]):
        return Response(status=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    return Response(data, headers={"ETag": etag})


snapshot.cls.required_scopes = {"default": ["pbx:sync"]}


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([])
def snapshot_schema(request):
    """``CREATE TABLE IF NOT EXISTS`` DDL for the venue database (``{"dialect": "postgresql", "sql": ...}``)."""
    from apps.pbx import snapshot as snap

    event, err = _sync_event_or_response(request, request.query_params.get("event"))
    if err:
        return err
    return Response({"dialect": "postgresql", "sql": snap.venue_schema_sql(),
                     "tables": [m._meta.db_table for m in snap.SCHEMA_MODELS]})


snapshot_schema.cls.required_scopes = {"default": ["pbx:sync"]}


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([])
def agent_heartbeat(request):
    """Venue agent check-in: ``{event, version, hostname, agent_version, asterisk_ok, message, contacts?}``.
    ``contacts`` (optional, ``ps_contacts`` rows) replace DIAL's copy for the event's endpoints. Not audited."""
    from apps.pbx import snapshot as snap

    body = request.data if isinstance(request.data, dict) else _flat(request.data)
    event, err = _sync_event_or_response(request, body.get("event") or request.query_params.get("event"))
    if err:
        return err
    conn = snap.apply_heartbeat(event, body)
    current = snap.snapshot_version(event)
    applied = str(body.get("version") or "")
    return Response({
        "ok": True, "event": event.slug, "current_version": current,
        "poll_interval": snap.poll_interval(event, conn), "behind": applied != current,
        "connection": conn is not None,
    })


agent_heartbeat.cls.required_scopes = {"default": ["pbx:sync"]}


urlpatterns = [
    path("pbx/hooks/<slug:kind>/", hook, name="pbx-hook"),
    path("pbx/route/", route, name="pbx-route"),
    path("pbx/dialplan/", dialplan_export, name="pbx-dialplan"),
    path("pbx/resync/", resync, name="pbx-resync"),
    path("pbx/status/", pbx_status, name="pbx-status"),
    path("pbx/outbox/", outbox_status, name="pbx-outbox"),
    path("pbx/outbox/retry/", outbox_retry, name="pbx-outbox-retry"),
    path("pbx/connection/", connection, name="pbx-connection"),
    path("pbx/snapshot/", snapshot, name="pbx-snapshot"),
    path("pbx/snapshot/schema/", snapshot_schema, name="pbx-snapshot-schema"),
    path("pbx/agent/heartbeat/", agent_heartbeat, name="pbx-agent-heartbeat"),
]
