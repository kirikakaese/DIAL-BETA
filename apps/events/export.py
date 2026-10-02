"""Full event export/import (JSON) for offline backup and migration between DIAL instances."""
from __future__ import annotations

from django.db import transaction
from django.utils.dateparse import parse_date, parse_datetime

from apps.devices.models import Device, DeviceBinding
from apps.extensions.models import Extension
from apps.numbering.models import NumberPlan, NumberRange
from apps.pages.models import InfoPage

from .models import Event, EventMembership, UserGroup

EXPORT_VERSION = 1

_EVENT_FIELDS = [
    "name", "slug", "description", "state", "is_public", "start_date", "end_date", "location", "timezone",
    "primary_color", "accent_color", "announcement", "sip_domain", "dial_prefix", "default_language",
    "max_extensions_per_user", "allow_guest_extensions", "allow_breakout", "cdr_aggregate_only",
    "cdr_retention_days", "settings", "registration_opens_at", "goes_live_at", "archives_at",
]
_PLAN_FIELDS = [f.name for f in NumberPlan._meta.fields if f.name not in ("id", "event", "created_at", "updated_at")]
_RANGE_FIELDS = [f.name for f in NumberRange._meta.fields if f.name not in ("id", "plan", "created_at", "updated_at")]
_EXT_FIELDS = [
    "number", "type", "state", "display_name", "description", "location_hint", "in_phonebook", "language",
    "ring_strategy", "ring_timeout", "forward_busy", "forward_noanswer", "forward_unconditional",
    "allow_callback", "priority", "is_temporary", "expires_at", "config", "request_note", "moderation_note",
]
_DEV_FIELDS = [
    "type", "name", "state", "ipei", "handset_model", "sip_username", "sip_password", "sip_transport",
    "mac_address", "imsi", "msisdn", "config", "notes",
]
_PAGE_FIELDS = ["slug", "title", "body", "order", "published", "show_on_dashboard"]


def _ser(obj, fields):
    out = {}
    for f in fields:
        v = getattr(obj, f)
        out[f] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


def export_event(event: Event) -> dict:
    plan = NumberPlan.objects.filter(event=event).first()
    data = {
        "version": EXPORT_VERSION,
        "event": _ser(event, _EVENT_FIELDS),
        "groups": [{"name": g.name, "slug": g.slug, "description": g.description, "join_code": g.join_code}
                   for g in event.groups.all()],
        "memberships": [{"user": m.user.email, "username": m.user.username, "role": m.role,
                         "groups": list(m.groups.values_list("slug", flat=True))}
                        for m in event.memberships.select_related("user")],
        "number_plan": _ser(plan, _PLAN_FIELDS) if plan else None,
        "ranges": [dict(_ser(r, _RANGE_FIELDS), allowed_groups=list(r.allowed_groups.values_list("slug", flat=True)))
                   for r in (plan.ranges.all() if plan else [])],
        "extensions": [dict(_ser(e, _EXT_FIELDS), owner=e.owner.email if e.owner else None,
                            devices=[{"device": str(b.device_id), "priority": b.priority, "ring_delay": b.ring_delay}
                                     for b in e.bindings.all()])
                       for e in event.extensions.exclude(state=Extension.State.DELETED)],
        "devices": [dict(_ser(d, _DEV_FIELDS), id=str(d.pk), owner=d.owner.email if d.owner else None)
                    for d in event.devices.all()],
        "pages": [_ser(p, _PAGE_FIELDS) for p in event.pages.all()],
    }
    # feature apps may contribute
    for hook in _export_hooks():
        data.update(hook(event))
    return data


def _export_hooks():
    hooks = []
    for mod_name in ("apps.callgroups.export", "apps.phonebook.export", "apps.ivr.export"):
        try:
            from importlib import import_module

            hooks.append(import_module(mod_name).export_event)
        except (ModuleNotFoundError, AttributeError):
            continue
    return hooks


@transaction.atomic
def import_event(data: dict, *, slug_override: str | None = None, actor=None) -> Event:
    from apps.accounts.models import User

    if data.get("version") != EXPORT_VERSION:
        raise ValueError("Unsupported export version")
    ev_data = dict(data["event"])
    if slug_override:
        ev_data["slug"] = slug_override
    ev_data["start_date"] = parse_date(ev_data["start_date"])
    ev_data["end_date"] = parse_date(ev_data["end_date"])
    for f in ("registration_opens_at", "goes_live_at", "archives_at"):
        if ev_data.get(f):
            ev_data[f] = parse_datetime(ev_data[f])
    if Event.objects.filter(slug=ev_data["slug"]).exists():
        raise ValueError(f"Event slug '{ev_data['slug']}' already exists")
    event = Event.objects.create(**ev_data)

    groups = {g["slug"]: UserGroup.objects.create(event=event, **g) for g in data.get("groups", [])}

    def user_for(email, username=None):
        if not email:
            return None
        u = User.objects.filter(email=email).first()
        if u is None:
            base = username or email.split("@")[0]
            uname = base
            i = 1
            while User.objects.filter(username=uname).exists():
                i += 1
                uname = f"{base}{i}"
            u = User.objects.create_user(email=email, username=uname, password=None)
            u.set_unusable_password()
            u.save()
        return u

    for m in data.get("memberships", []):
        u = user_for(m["user"], m.get("username"))
        mem = EventMembership.objects.create(event=event, user=u, role=m["role"])
        mem.groups.set([groups[s] for s in m.get("groups", []) if s in groups])

    if data.get("number_plan"):
        plan = NumberPlan.objects.create(event=event, **data["number_plan"])
        for r in data.get("ranges", []):
            r = dict(r)
            ag = r.pop("allowed_groups", [])
            rng = NumberRange.objects.create(plan=plan, **r)
            rng.allowed_groups.set([groups[s] for s in ag if s in groups])

    dev_map = {}
    for d in data.get("devices", []):
        d = dict(d)
        old_id = d.pop("id")
        owner = user_for(d.pop("owner", None))
        dev_map[old_id] = Device.objects.create(event=event, owner=owner, **d)

    for e in data.get("extensions", []):
        e = dict(e)
        owner = user_for(e.pop("owner", None))
        binds = e.pop("devices", [])
        if e.get("expires_at"):
            e["expires_at"] = parse_datetime(e["expires_at"])
        ext = Extension.objects.create(event=event, owner=owner, **e)
        for b in binds:
            dev = dev_map.get(b["device"])
            if dev:
                DeviceBinding.objects.create(extension=ext, device=dev, priority=b["priority"],
                                             ring_delay=b["ring_delay"])

    for p in data.get("pages", []):
        InfoPage.objects.create(event=event, **{k: p[k] for k in _PAGE_FIELDS if k in p})
    from apps.core.audit import log

    log(action="create", actor=actor, target=event, event=event, message="Imported from backup")
    return event
