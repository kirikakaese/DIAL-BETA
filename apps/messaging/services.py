"""Messaging services: deliver text to DECT handsets through the DECT adapter.

Contract: every function is a no-op (returns ``None`` / empty list) when the ``messaging``
feature flag is off for the event.
"""
from __future__ import annotations

import logging

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.audit import log
from apps.core.features import enabled
from apps.dect import get_dect
from apps.devices.models import Device
from apps.extensions.models import ENDPOINT_TYPES, Extension

from .models import MAX_TEXT, Broadcast, Message

logger = logging.getLogger("dial.messaging")


class MessagingError(Exception):
    pass


def _check(event, text: str):
    if not enabled("messaging", event):
        raise MessagingError(_("Messaging is disabled for this event."))
    text = (text or "").strip()
    if not text:
        raise MessagingError(_("Message text is empty."))
    if len(text) > MAX_TEXT:
        raise MessagingError(_("Message is too long (max %(n)s characters).") % {"n": MAX_TEXT})
    return text


def dect_devices(extension: Extension) -> list[Device]:
    """DECT devices bound to ``extension`` that are known to the OMM (``omm_ppn`` set)."""
    return [b.device for b in extension.bindings.filter(is_active=True).select_related("device")
            if b.device.type == "dect" and b.device.omm_ppn]


def deliver(msg: Message, priority: str = "normal") -> Message:
    """Push one stored outbound message to all handsets of its recipient extension."""
    ext = msg.recipient_extension
    devices = dect_devices(ext) if ext is not None else []
    if not devices:
        msg.state, msg.error = Message.State.FAILED, "no DECT handset bound to extension"
        msg.save(update_fields=["state", "error", "updated_at"])
        return msg
    dect = get_dect(ext.event)
    ok = False
    errors = []
    for dev in devices:
        try:
            if dect.send_message(ppn=dev.omm_ppn, text=msg.text, priority=priority):
                ok = True
            else:
                errors.append(f"ppn {dev.omm_ppn} rejected")
        except Exception as exc:  # noqa: BLE001 - adapter failure must not break the caller
            logger.warning("send_message to %s failed: %s", dev.omm_ppn, exc)
            errors.append(str(exc)[:80])
    msg.state = Message.State.SENT if ok else Message.State.FAILED
    msg.sent_at = timezone.now() if ok else None
    msg.error = "" if ok else "; ".join(errors)[:200]
    msg.save(update_fields=["state", "sent_at", "error", "updated_at"])
    return msg


def send_to_extension(event, sender, extension: Extension, text: str, *, sender_extension=None,
                      priority: str = "normal", broadcast: Broadcast | None = None) -> Message | None:
    if not enabled("messaging", event):
        return None
    text = _check(event, text)
    if extension.event_id != event.pk:
        raise MessagingError(_("Extension does not belong to this event."))
    if sender_extension is None and sender is not None:
        sender_extension = Extension.objects.filter(event=event, owner=sender).active().first()
    msg = Message.objects.create(event=event, sender=sender, sender_extension=sender_extension,
                                 recipient_extension=extension, text=text, broadcast=broadcast)
    return deliver(msg, priority=priority)


def group_extensions(event, user_group):
    """Active endpoint extensions owned by members of ``user_group``."""
    user_ids = user_group.members.values_list("user_id", flat=True)
    return Extension.objects.filter(event=event, owner_id__in=user_ids, type__in=ENDPOINT_TYPES).active()


def send_to_group(event, sender, user_group, text: str, *, priority: str = "normal",
                  broadcast: Broadcast | None = None) -> list[Message]:
    if not enabled("messaging", event):
        return []
    text = _check(event, text)
    out = []
    for ext in group_extensions(event, user_group):
        msg = Message.objects.create(event=event, sender=sender, recipient_extension=ext, recipient_group=user_group,
                                     text=text, broadcast=broadcast)
        out.append(deliver(msg, priority=priority))
    return out


def broadcast(event, sender, text: str, group=None, *, priority: str = "high", request=None) -> Broadcast | None:
    """Orga broadcast: text to every active endpoint extension (or the members of ``group``)."""
    if not enabled("messaging", event):
        return None
    text = _check(event, text)
    bc = Broadcast.objects.create(event=event, sender=sender, text=text,
                                  target=Broadcast.Target.GROUP if group else Broadcast.Target.ALL, group=group)
    if group is not None:
        msgs = send_to_group(event, sender, group, text, priority=priority, broadcast=bc)
    else:
        exts = Extension.objects.filter(event=event, type__in=ENDPOINT_TYPES).active()
        msgs = [deliver(Message.objects.create(event=event, sender=sender, recipient_extension=e, text=text,
                                               broadcast=bc), priority=priority) for e in exts]
    bc.sent_count = sum(1 for m in msgs if m.state == Message.State.SENT)
    bc.failed_count = len(msgs) - bc.sent_count
    bc.save(update_fields=["sent_count", "failed_count", "updated_at"])
    log(action="other", actor=sender, target=bc, event=event, request=request,
        message=f"Broadcast to {bc.target}: {bc.sent_count} sent, {bc.failed_count} failed")
    return bc


def handle_inbound(event, from_ppn: str, text: str) -> Message | None:
    """OMM → DIAL gateway hook (placeholder): store a message sent *from* a handset.

    Wire this to the OMM message event stream (AXI ``MessageIndication``) in the DECT backend.
    """
    if not enabled("messaging", event):
        return None
    dev = Device.objects.filter(event=event, omm_ppn=str(from_ppn)).first()
    ext = None
    if dev is not None:
        binding = dev.bindings.filter(is_active=True).select_related("extension").first()
        ext = binding.extension if binding else None
    return Message.objects.create(event=event, direction=Message.Direction.IN, sender=getattr(dev, "owner", None),
                                  sender_extension=ext, text=(text or "")[:MAX_TEXT],
                                  state=Message.State.DELIVERED, sent_at=timezone.now())
