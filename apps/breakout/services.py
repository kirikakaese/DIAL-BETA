"""Breakout services.

PBX contract: ``authorize(event, extension, destination) -> (bool, reason)``. ``destination`` is
the digits the user dialed (including the trunk's ``outbound_prefix``); the PBX dials
``dial_string(trunk, destination)`` when allowed.
"""
from __future__ import annotations

import math
import re

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.core.features import enabled
from apps.events.models import EventMembership

from .models import BreakoutPermission, BreakoutUsage, CallerIdMapping, OutboundRule, Trunk

DEFAULT_BLOCKLIST = ["0900", "0137", "0180", "0190", "00", "1900", "118", "0700"]
RATE_LIMIT_CALLS = 5          # per extension ...
RATE_LIMIT_WINDOW = 600       # ... per 10 minutes


def blocklist() -> list[str]:
    return list(getattr(settings, "DIAL_BREAKOUT_BLOCKLIST", DEFAULT_BLOCKLIST))


def trunk_for(event, destination: str, extension=None) -> Trunk | None:
    """Enabled trunk for ``destination``: the one named in a ``breakout`` extension's ``config["trunk"]``
    (endpoint name, pk or name), else the longest ``outbound_prefix`` match."""
    hint = str((getattr(extension, "config", None) or {}).get("trunk") or "")
    if hint:
        qs = Trunk.objects.filter(event=event, enabled=True)
        t = qs.filter(name=hint).first() or (qs.filter(pk=hint[6:]).first() if hint.startswith("trunk-")
                                             and hint[6:].isdigit() else None)
        if t is not None:
            return t
    best = None
    for t in Trunk.objects.filter(event=event, enabled=True):
        if destination.startswith(t.outbound_prefix) and len(destination) > len(t.outbound_prefix):
            if best is None or len(t.outbound_prefix) > len(best.outbound_prefix):
                best = t
    return best


def strip_prefix(trunk: Trunk, destination: str) -> str:
    return destination[len(trunk.outbound_prefix):] if destination.startswith(trunk.outbound_prefix) else destination


def permission_for(event, extension) -> BreakoutPermission | None:
    """Extension-specific permission first, then the most generous group permission."""
    p = BreakoutPermission.objects.filter(event=event, extension=extension).first()
    if p is not None:
        return p
    owner_id = getattr(extension, "owner_id", None)
    if not owner_id:
        return None
    m = EventMembership.objects.filter(event=event, user_id=owner_id).first()
    if m is None:
        return None
    perms = list(BreakoutPermission.objects.filter(event=event, user_group__in=m.groups.all()))
    if not perms:
        return None
    allowed = [p for p in perms if p.allowed]
    if not allowed:
        return perms[0]
    return sorted(allowed, key=lambda p: (p.daily_minutes_limit != 0, -p.daily_minutes_limit))[0]


def matching_rule(trunk: Trunk, number: str) -> OutboundRule | None:
    for rule in trunk.rules.all():
        try:
            if re.fullmatch(rule.pattern, number):
                return rule
        except re.error:
            continue
    return None


def minutes_used_today(event, extension) -> float:
    row = BreakoutUsage.objects.filter(event=event, extension=extension, date=timezone.localdate()).first()
    return row.minutes if row else 0.0


def _rate_key(extension) -> str:
    return f"breakout:rate:{extension.pk}"


def rate_limited(extension) -> bool:
    limit = int(getattr(settings, "DIAL_BREAKOUT_RATE_LIMIT", RATE_LIMIT_CALLS))
    return (cache.get(_rate_key(extension)) or 0) >= limit


def count_attempt(extension) -> None:
    key = _rate_key(extension)
    if cache.add(key, 1, RATE_LIMIT_WINDOW):
        return
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, RATE_LIMIT_WINDOW)


def authorize(event, extension, destination: str) -> tuple[bool, str]:
    destination = re.sub(r"\D", "", str(destination or ""))
    if not enabled("breakout", event):
        return False, "breakout disabled"
    if not event.allow_breakout:
        return False, "breakout not allowed for this event"
    if extension is None or not destination:
        return False, "unknown extension or destination"
    trunk = trunk_for(event, destination, extension)
    if trunk is None:
        return False, "no trunk for this prefix"
    number = strip_prefix(trunk, destination)
    for blocked in blocklist():
        if number.startswith(blocked):
            return False, f"destination blocked ({blocked})"
    perm = permission_for(event, extension)
    if perm is None or not perm.allowed:
        return False, "extension not permitted for breakout"
    rule = matching_rule(trunk, number)
    if rule is not None and not rule.allow:
        return False, f"denied by rule {rule.name}"
    if trunk.rules.filter(allow=True).exists() and (rule is None or not rule.allow):
        return False, "no allow rule matches"
    if perm.daily_minutes_limit and minutes_used_today(event, extension) >= perm.daily_minutes_limit:
        return False, "daily minute quota exhausted"
    if rate_limited(extension):
        return False, "rate limit: too many breakout calls"
    count_attempt(extension)
    return True, "ok"


def caller_id_for(extension, trunk: Trunk) -> str:
    m = CallerIdMapping.objects.filter(trunk=trunk, extension=extension).first()
    return m.caller_id if m else trunk.caller_id_default


def dial_string(trunk: Trunk, destination: str) -> str:
    return f"PJSIP/{strip_prefix(trunk, re.sub(r'\D', '', destination))}@{trunk.endpoint_name}"


def record_usage(event, extension, billsec: int) -> BreakoutUsage:
    """Add one call of ``billsec`` seconds (rounded up to minutes) to today's usage row."""
    row, _c = BreakoutUsage.objects.get_or_create(event=event, extension=extension, date=timezone.localdate())
    row.calls += 1
    row.minutes += math.ceil(max(int(billsec or 0), 0) / 60)
    row.save(update_fields=["calls", "minutes"])
    return row


def pjsip_trunk_config(trunk: Trunk) -> str:
    name = trunk.endpoint_name
    lines = [
        f"; DIAL breakout trunk: {trunk.name}",
        f"[{name}]", "type=endpoint", f"transport=transport-{trunk.transport}", "context=dial-breakout-in",
        "disallow=all", "allow=alaw,ulaw,g722", f"aors={name}",
        f"from_domain={trunk.from_domain or trunk.sip_host}",
        f"media_encryption={'sdes' if trunk.transport == 'tls' else 'no'}",
        "rtp_symmetric=yes", "force_rport=yes", "rewrite_contact=yes", "direct_media=no", "send_pai=yes",
    ]
    if trunk.caller_id_default:
        lines.append(f'callerid="DIAL" <{trunk.caller_id_default}>')
    if trunk.auth_user:
        lines.append(f"outbound_auth={name}-auth")
    lines += ["", f"[{name}]", "type=aor", f"contact=sip:{trunk.sip_host}:{trunk.port};transport={trunk.transport}",
              "qualify_frequency=60", "", f"[{name}]", "type=identify", f"endpoint={name}", f"match={trunk.sip_host}"]
    if trunk.auth_user:
        lines += ["", f"[{name}-auth]", "type=auth", "auth_type=userpass", f"username={trunk.auth_user}",
                  f"password={trunk.auth_password}"]
        lines += ["", f"[{name}-reg]", "type=registration", f"outbound_auth={name}-auth",
                  f"server_uri=sip:{trunk.sip_host}:{trunk.port}",
                  f"client_uri=sip:{trunk.auth_user}@{trunk.from_domain or trunk.sip_host}", "retry_interval=60"]
    return "\n".join(lines) + "\n"
