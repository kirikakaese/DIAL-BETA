"""Dialplan generation for the Asterisk backend.

Everything here is pure: it turns PET models into ``(exten, priority, app,
appdata)`` rows for the realtime ``extensions`` table (or a static
``extensions.conf`` snippet). The per-event context is ``pet-<slug>``; the
static contexts it refers to (``pet-services``, ``pet-group``, ``pet-ivr``,
``pet-feature``, ``pet-hangup``, ...) live in ``deploy/asterisk/conf/extensions.conf``.

How a call to ``4242`` flows (parallel ring, two devices)::

    [pet-demo]                      <- endpoint context (set on ps_endpoints.context)
    4242,1  Set(__PET_EVENT=demo)
    4242,2  Set(PET_EXTEN=4242)
    4242,3  Set(PET_ALLOW_CB=1)
    4242,4  Set(PET_PRIORITY=0)
    4242,5  Set(CHANNEL(hangup_handler_push)=pet-hangup,s,1)
    4242,6  Dial(PJSIP/demo-aaaa&PJSIP/demo-bbbb,30,tT)
    4242,7  GotoIf($["${DIALSTATUS}" = "BUSY"]?10)
    4242,8  VoiceMail(4242@pet-demo,u)         (or Goto(pet-demo,<forward_noanswer>,1))
    4242,9  Hangup()
    4242,10 VoiceMail(4242@pet-demo,b)         (or Goto(pet-demo,<forward_busy>,1) / Busy(10))
    4242,11 Hangup()

Per-extension features change individual rows:

- ``language`` set → an extra ``Set(CHANNEL(language)=de)`` in the preamble (else the event default).
- ready ``ringback_tone`` → the Dial gets ``m(pet-<slug>-<number>)`` so the caller hears the custom
  tone (a music-on-hold class rendered by ``AsteriskPBX.render_musiconhold``) instead of ringing.
- ``forward_mode``/``forward_target`` (FK): ``always`` → ``Goto(ctx,<target>,1)`` instead of Dial;
  ``delayed`` → Dial with timeout ``forward_delay`` then ``Goto``; ``busy``/``noanswer`` → ``Goto``
  in the respective branch. The legacy free-text ``forward_*`` fields apply when the mode is ``off``.
- ``trunk`` extensions (number blocks, see :func:`rows_for_trunk`) get two extens: the base number and the
  block pattern ``_47XX``, both dialling ``PJSIP/<number>@<sip account of the remote PBX>``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from apps.extensions.models import ENDPOINT_TYPES, Extension, ExtensionType

CONTEXT_PREFIX = "pet-"
SERVICES_CONTEXT = "pet-services"
INTERNAL_CONTEXT = "pet-internal"
HANGUP_HANDLER = "pet-hangup,s,1"

# Service number field on NumberPlan -> exten in [pet-services]
SERVICE_NUMBERS = [
    ("echo_test_number", "echo"),
    ("test_ringback_number", "ringback-request"),
    ("wakeup_service_number", "wakeup"),
    ("site_survey_number", "survey"),
    ("voicemail_number", "voicemail"),
    ("dect_claim_number", "dect-claim"),
    ("announcement_record_number", "record-announcement"),
]
# Service numbers that also accept trailing digits (``<number><code>``), passed on in the given channel variable
SERVICE_WITH_SUFFIX = {"dect_claim_number": "PET_CLAIM_CODE", "announcement_record_number": "PET_RECORD_CODE"}
FEATURE_CODES = ["callback_request_code", "callback_cancel_code", "group_login_code", "group_logout_code",
                 "forward_set_code", "forward_clear_code", "forward_busy_code", "forward_noanswer_code"]

_LABEL = re.compile(r"\{\{L:([a-z_]+)\}\}")


@dataclass(frozen=True)
class Row:
    exten: str
    priority: int
    app: str
    appdata: str = ""

    def as_conf_line(self) -> str:
        return f"exten => {self.exten},{self.priority},{self.app}({self.appdata})"


def event_context(event) -> str:
    return f"{CONTEXT_PREFIX}{event.slug}"


def conference_name(ext: Extension) -> str:
    return f"{CONTEXT_PREFIX}{ext.event.slug}-{ext.number}"


def mailbox(ext: Extension) -> str:
    return f"{ext.number}@{event_context(ext.event)}"


def voicemail_enabled(event) -> bool:
    from apps.core.features import enabled

    return enabled("voicemail", event)


def dial_string(device) -> str:
    """Technology/resource string for one device (via the endpoint-type registry)."""
    from apps.devices import endpoint_types

    try:
        return endpoint_types.get(device.type).dial(device)
    except KeyError:
        return f"PJSIP/{device.sip_username}"


def dial_targets(ext: Extension) -> list[tuple[object, str]]:
    """``[(binding, dial_string)]`` for the active, dialable bindings of ``ext`` (serial order)."""
    out = []
    qs = ext.bindings.filter(is_active=True).select_related("device").order_by("priority", "id")
    for b in qs:
        d = b.device
        if d.state == d.State.DISABLED:
            continue
        if d.type == "gsm":
            if not d.msisdn:
                continue
        elif not d.sip_username:
            continue
        out.append((b, dial_string(d)))
    return out


def trunk_endpoint(ext: Extension) -> str:
    """PJSIP endpoint name (``device.sip_username``) of the SIP account bound to trunk ``ext``, or ``""``."""
    qs = ext.bindings.filter(is_active=True, device__type="sip").select_related("device").order_by("priority", "id")
    for b in qs:
        if b.device.state != b.device.State.DISABLED and b.device.sip_username:
            return b.device.sip_username
    return ""


def trunk_dial_string(ext: Extension, number: str) -> str:
    """``PJSIP/<number>@<endpoint>`` - how a number of the trunk's block is handed to the remote PBX."""
    endpoint = trunk_endpoint(ext)
    return f"PJSIP/{number}@{endpoint}" if endpoint else ""


def extens_for_extension(ext: Extension) -> set[str]:
    """Every exten :func:`rows_for_extension` writes for ``ext`` (the number, plus the block pattern for trunks)."""
    out = {ext.number}
    if ext.is_trunk and ext.block_digits:
        out.add(ext.block_pattern)
    return out


class ExtenBuilder:
    """Collects rows for one exten; forward label references are resolved on ``build()``."""

    def __init__(self, exten: str):
        self.exten = exten
        self._rows: list[tuple[str, str]] = []
        self._labels: dict[str, int] = {}

    @property
    def next_priority(self) -> int:
        return len(self._rows) + 1

    def add(self, app: str, appdata: str = "") -> int:
        self._rows.append((app, appdata))
        return len(self._rows)

    def label(self, name: str) -> int:
        """Name the *next* priority."""
        self._labels[name] = self.next_priority
        return self.next_priority

    def ref(self, name: str) -> str:
        return f"{{{{L:{name}}}}}"

    def build(self) -> list[Row]:
        def resolve(m):
            try:
                return str(self._labels[m.group(1)])
            except KeyError:
                raise ValueError(f"unresolved dialplan label {m.group(1)!r} in exten {self.exten}")

        return [Row(self.exten, i + 1, app, _LABEL.sub(resolve, data))
                for i, (app, data) in enumerate(self._rows)]


# --------------------------------------------------------------------------- extensions

def _preamble(b: ExtenBuilder, ext: Extension, ctx: str, exten: str | None = None):
    b.add("Set", f"__PET_EVENT={ext.event.slug}")
    b.add("Set", f"PET_EXTEN={exten or ext.number}")
    b.add("Set", f"PET_ALLOW_CB={1 if ext.allow_callback else 0}")
    b.add("Set", f"PET_PRIORITY={ext.priority}")
    b.add("Set", f"CHANNEL(hangup_handler_push)={HANGUP_HANDLER}")
    lang = announcement_language(ext)
    if lang:
        b.add("Set", f"CHANNEL(language)={lang}")


def announcement_language(ext: Extension) -> str:
    """Prompt language for calls to ``ext``: its own setting, else the event default."""
    return (ext.language or ext.event.default_language or "").strip()


def forward_number(ext: Extension, mode: str) -> str:
    """Number to forward to for ``mode`` (``always``/``busy``/``noanswer``/``delayed``), or ``""``.

    FK-based forwarding wins; legacy free-text fields apply while ``forward_mode`` is ``off``.
    """
    fm = Extension.ForwardMode
    if ext.forward_mode != fm.OFF:
        if ext.forward_mode == mode and ext.forward_target_id:
            return ext.forward_target.number
        return ""
    legacy = {fm.ALWAYS: ext.forward_unconditional, fm.BUSY: ext.forward_busy, fm.NOANSWER: ext.forward_noanswer}
    return (legacy.get(mode) or "").strip()


def dial_options(ext: Extension) -> str:
    opts = "tT"
    if ext.has_ringback_tone:
        opts += f"m({ext.ringback_class})"
    return opts


def _noanswer_branch(b: ExtenBuilder, ext: Extension, ctx: str, vm: bool):
    b.label("noanswer")
    fwd = forward_number(ext, Extension.ForwardMode.NOANSWER) or forward_number(ext, Extension.ForwardMode.DELAYED)
    if fwd:
        b.add("Goto", f"{ctx},{fwd},1")
    elif vm:
        b.add("VoiceMail", f"{mailbox(ext)},u")
    else:
        b.add("Playback", "vm-nobodyavail")
    b.add("Hangup")


def _busy_branch(b: ExtenBuilder, ext: Extension, ctx: str, vm: bool):
    b.label("busy")
    fwd = forward_number(ext, Extension.ForwardMode.BUSY) or forward_number(ext, Extension.ForwardMode.DELAYED)
    if fwd:
        b.add("Goto", f"{ctx},{fwd},1")
    elif vm:
        b.add("VoiceMail", f"{mailbox(ext)},b")
    else:
        b.add("Busy", "10")
    b.add("Hangup")


def _endpoint_rows(b: ExtenBuilder, ext: Extension, ctx: str):
    always = forward_number(ext, Extension.ForwardMode.ALWAYS)
    if always:
        b.add("Goto", f"{ctx},{always},1")
        return
    vm = voicemail_enabled(ext.event)
    targets = dial_targets(ext)
    delayed = forward_number(ext, Extension.ForwardMode.DELAYED)
    timeout = (ext.forward_delay or 10) if delayed else (ext.ring_timeout or 30)
    opts = dial_options(ext)
    if not targets:
        b.add("NoOp", f"PET: no devices bound to {ext.number}")
        b.add("Goto", b.ref("noanswer"))
    elif ext.ring_strategy == Extension.RingStrategy.SERIAL and len(targets) > 1:
        for binding, ds in targets:
            if binding.ring_delay:
                b.add("Wait", str(binding.ring_delay))
            b.add("Dial", f"{ds},{timeout},{opts}")
            b.add("GotoIf", f'$["${{DIALSTATUS}}" = "BUSY"]?{b.ref("busy")}')
        b.add("Goto", b.ref("noanswer"))
    else:
        b.add("Dial", f"{'&'.join(ds for _, ds in targets)},{timeout},{opts}")
        b.add("GotoIf", f'$["${{DIALSTATUS}}" = "BUSY"]?{b.ref("busy")}')
    _noanswer_branch(b, ext, ctx, vm)
    _busy_branch(b, ext, ctx, vm)


def _trunk_rows(b: ExtenBuilder, ext: Extension, ctx: str, number: str):
    """Hand ``number`` (a literal or ``${EXTEN}``) to the remote PBX registered on the trunk's SIP account."""
    b.add("Set", f"PET_TRUNK={ext.number}")
    ds = trunk_dial_string(ext, number)
    if not ds:
        b.add("NoOp", f"PET: no SIP account bound to trunk {ext.number}")
        b.add("Playback", "vm-nobodyavail")
        b.add("Hangup")
        return
    b.add("Dial", f"{ds},{ext.ring_timeout or 30},{dial_options(ext)}")
    b.add("GotoIf", f'$["${{DIALSTATUS}}" = "BUSY"]?{b.ref("busy")}')
    b.add("Playback", "vm-nobodyavail")
    b.add("Hangup")
    b.label("busy")
    b.add("Busy", "10")
    b.add("Hangup")


def rows_for_trunk(ext: Extension) -> list[Row]:
    """Two extens for a trunk: the base number (``4700`` -> ``PJSIP/4700@ep``) and the block pattern
    (``_47XX`` -> ``PJSIP/${EXTEN}@ep``). Asterisk prefers the exact match, so the base wins over the pattern."""
    ctx = event_context(ext.event)
    b = ExtenBuilder(ext.number)
    _preamble(b, ext, ctx)
    _trunk_rows(b, ext, ctx, ext.number)
    rows = b.build()
    if ext.block_digits:
        p = ExtenBuilder(ext.block_pattern)
        _preamble(p, ext, ctx, exten="${EXTEN}")
        _trunk_rows(p, ext, ctx, "${EXTEN}")
        rows += p.build()
    return rows


def rows_for_extension(ext: Extension) -> list[Row]:
    """Realtime rows for one active extension in its event context."""
    if ext.is_trunk:
        return rows_for_trunk(ext)
    ctx = event_context(ext.event)
    b = ExtenBuilder(ext.number)
    _preamble(b, ext, ctx)
    t = ext.type
    cfg = ext.config or {}
    if t in ENDPOINT_TYPES:
        _endpoint_rows(b, ext, ctx)
    elif t == ExtensionType.GROUP:
        b.add("Gosub", f"pet-group,s,1({ext.number})")
        b.add("Hangup")
    elif t in (ExtensionType.ANNOUNCEMENT, ExtensionType.IVR):
        b.add("Gosub", f"pet-ivr,s,1({ext.number},{t})")
        b.add("Hangup")
    elif t == ExtensionType.CONFERENCE:
        b.add("Answer")
        pin = str(cfg.get("pin") or "").strip()
        if pin:
            b.add("Authenticate", pin)
        b.add("ConfBridge", f"{conference_name(ext)},pet_bridge,pet_user")
        b.add("Hangup")
    elif t == ExtensionType.VOICEMAIL:
        b.add("Answer")
        b.add("VoiceMail", f"{mailbox(ext)},u")
        b.add("Hangup")
    elif t == ExtensionType.APP:
        b.add("Gosub", f"pet-app,s,1({cfg.get('app', 'echo')})")
        b.add("Hangup")
    elif t in (ExtensionType.FEDERATION, ExtensionType.BREAKOUT):
        b.add("Gosub", f"pet-route,s,1({ext.number})")
        b.add("Hangup")
    else:
        b.add("Playback", "pbx-invalid")
        b.add("Hangup")
    return b.build()


# --------------------------------------------------------------------------- plan rows

def rows_for_plan(event, plan) -> list[Row]:
    """Service numbers, feature codes and emergency numbers of the event's number plan."""
    slug = event.slug
    rows: list[Row] = []
    seen: set[str] = set()

    def pre(b):
        b.add("Set", f"__PET_EVENT={slug}")

    for field, svc in SERVICE_NUMBERS:
        number = (getattr(plan, field, "") or "").strip()
        if not number or number in seen:
            continue
        seen.add(number)
        b = ExtenBuilder(number)
        pre(b)
        b.add("Goto", f"{SERVICES_CONTEXT},{svc},1")
        rows += b.build()
        var = SERVICE_WITH_SUFFIX.get(field)
        if var:
            b = ExtenBuilder(f"_{number}X.")  # number + code dialled en bloc, e.g. 9004123456
            pre(b)
            b.add("Set", f"{var}=${{EXTEN:{len(number)}}}")
            b.add("Goto", f"{SERVICES_CONTEXT},{svc},1")
            rows += b.build()

    for field in FEATURE_CODES:
        code = (getattr(plan, field, "") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        b = ExtenBuilder(code)  # bare code, e.g. *86 = cancel all
        pre(b)
        b.add("Gosub", f"pet-feature,s,1({code},)")
        b.add("Hangup")
        rows += b.build()
        b = ExtenBuilder(f"_{code}.")  # code + target, e.g. *664242
        pre(b)
        b.add("Gosub", f"pet-feature,s,1({code},${{EXTEN:{len(code)}}})")
        b.add("Hangup")
        rows += b.build()

    for number in plan.emergency_numbers or []:
        number = str(number).strip()
        if not number or number in seen:
            continue
        seen.add(number)
        b = ExtenBuilder(number)
        pre(b)
        b.add("Set", "PET_PRIORITY=100")
        b.add("Goto", f"pet-emergency,{number},1")
        rows += b.build()
    return rows


def plan_extens(plan) -> set[str]:
    """All extens ``rows_for_plan`` would produce (used to prune stale rows)."""
    out = set()
    for field, _svc in SERVICE_NUMBERS:
        v = (getattr(plan, field, "") or "").strip()
        if v:
            out.add(v)
            if field in SERVICE_WITH_SUFFIX:
                out.add(f"_{v}X.")
    for field in FEATURE_CODES:
        v = (getattr(plan, field, "") or "").strip()
        if v:
            out.add(v)
            out.add(f"_{v}.")
    for n in plan.emergency_numbers or []:
        if str(n).strip():
            out.add(str(n).strip())
    return out


# --------------------------------------------------------------------------- rendering

def shell_context(event) -> str:
    """The static shell every event needs so that ``switch => Realtime/@`` resolves."""
    ctx = event_context(event)
    return f"[{ctx}]\ninclude => {INTERNAL_CONTEXT}\nswitch => Realtime/@\n"


def render_context(event, rows: list[Row], *, header=True) -> str:
    ctx = event_context(event)
    lines = []
    if header:
        lines.append(f"; PET dialplan for event '{event.slug}' - generated "
                     f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC")
        lines.append("; static equivalent of the realtime rows in table 'extensions'")
    lines.append(f"[{ctx}]")
    lines.append(f"include => {INTERNAL_CONTEXT}")
    last = None
    for r in sorted(rows, key=lambda r: (r.exten, r.priority)):
        if r.exten != last:
            lines.append("")
            last = r.exten
        lines.append(r.as_conf_line())
    lines.append("")
    return "\n".join(lines)
