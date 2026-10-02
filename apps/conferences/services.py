"""Conference services: room lifecycle, live participant refresh (best-effort) and kick."""
from __future__ import annotations

import re

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.audit import log
from apps.core.features import enabled
from apps.extensions.models import ExtensionType
from apps.extensions.services import register
from apps.pbx import get_pbx
from apps.pbx.base import PBXError

from .models import ConferenceParticipant, ConferenceRoom

ROOM_FIELDS = ("name", "pin", "max_participants", "record", "music_on_hold", "announce_join", "is_public")


class ConferenceError(Exception):
    pass


def _validate_pin(pin: str) -> str:
    pin = (pin or "").strip()
    if pin and not re.fullmatch(r"\d{4,8}", pin):
        raise ConferenceError(_("PIN must be 4-8 digits."))
    return pin


def sync_config(room: ConferenceRoom) -> None:
    """Keep ``Extension.config`` in sync - the PBX route API reads ``config['pin']``."""
    ext = room.extension
    ext.config = {**(ext.config or {}), "pin": room.pin, "max": room.max_participants, "record": room.record,
                  "moh": room.music_on_hold, "announce_join": room.announce_join}
    ext.save(update_fields=["config", "updated_at"])


def create_room(event, owner, number: str, pin: str = "", *, request=None, **fields) -> ConferenceRoom:
    if not enabled("conferences", event):
        raise ConferenceError(_("Conferences are disabled for this event."))
    pin = _validate_pin(pin)
    room_fields = {k: fields.pop(k) for k in list(fields) if k in ROOM_FIELDS}
    ext = register(event, owner, number, ExtensionType.CONFERENCE, request=request,
                   config={"pin": pin, "max": room_fields.get("max_participants", 20)}, **fields)
    room = ConferenceRoom.objects.create(extension=ext, owner=owner, pin=pin, **room_fields)
    sync_config(room)
    return room


def update_room(room: ConferenceRoom, actor=None, request=None, **fields) -> ConferenceRoom:
    if "pin" in fields:
        fields["pin"] = _validate_pin(fields["pin"])
    changes = {}
    for k, v in fields.items():
        if k in ROOM_FIELDS and getattr(room, k) != v:
            changes[k] = [getattr(room, k), v]
            setattr(room, k, v)
    if changes:
        room.save()
        sync_config(room)
        log(action="update", actor=actor, target=room.extension, event=room.event, request=request,
            changes={k: ["***" if k == "pin" else a, "***" if k == "pin" else b] for k, (a, b) in changes.items()})
    return room


def _matches_room(ch, room: ConferenceRoom) -> bool:
    from apps.pbx import dialplan as dp

    bridge = str(ch.extra.get("bridge") or ch.extra.get("conference") or "")
    return ch.callee == room.number or bridge in (room.number, dp.conference_name(room.extension))


def refresh_participants(room: ConferenceRoom) -> list[ConferenceParticipant]:
    """Reconcile participants with ``active_channels()``; tolerate PBX errors (returns current state)."""
    if not enabled("conferences", room.event):
        return []
    try:
        channels = [c for c in get_pbx(room.event).active_channels(room.event) if _matches_room(c, room)]
    except PBXError:
        return list(room.active_participants)
    live_ids = {c.id for c in channels}
    now = timezone.now()
    for p in room.active_participants:
        if p.channel_id not in live_ids:
            p.left_at = now
            p.save(update_fields=["left_at", "updated_at"])
    known = set(room.active_participants.values_list("channel_id", flat=True))
    for c in channels:
        if c.id not in known:
            ConferenceParticipant.objects.create(room=room, channel_id=c.id, caller_number=c.caller or "")
    return list(room.active_participants)


def kick(participant: ConferenceParticipant, actor=None, request=None) -> bool:
    try:
        get_pbx(participant.room.event).hangup(participant.channel_id)
        ok = True
    except PBXError:
        ok = False
    participant.left_at = timezone.now()
    participant.save(update_fields=["left_at", "updated_at"])
    log(action="other", actor=actor, target=participant.room.extension, event=participant.room.event,
        request=request, message=f"Kicked {participant.caller_number} ({participant.channel_id})")
    return ok


def visible_rooms(event, user):
    from django.db.models import Q

    qs = ConferenceRoom.objects.filter(extension__event=event).select_related("extension", "owner")
    if user.is_orga(event):
        return qs
    return qs.filter(Q(is_public=True) | Q(owner=user))
