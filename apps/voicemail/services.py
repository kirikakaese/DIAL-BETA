"""Voicemail services: mailbox provisioning (incl. Asterisk realtime row), message intake from the PBX
hook, MWI fan-out to PBX + DECT, e-mail delivery, read/delete bookkeeping."""
from __future__ import annotations

import logging
import os
import secrets
from datetime import UTC, datetime

from django.core.files import File
from django.core.files.storage import default_storage
from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.audit import log as audit
from apps.core.features import enabled
from apps.dect import get_dect
from apps.extensions.models import ENDPOINT_TYPES, Extension, ExtensionType
from apps.pbx import dialplan as dp
from apps.pbx import get_pbx
from apps.pbx.models import VoicemailUser

from .models import Mailbox, Message, MessageDelivery, default_from_email

log = logging.getLogger("pet.voicemail")

MAILBOX_TYPES = tuple(ENDPOINT_TYPES) + (ExtensionType.VOICEMAIL, ExtensionType.GROUP)


class VoicemailError(Exception):
    pass


def _new_pin() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(4))


def validate_pin(pin: str) -> str:
    pin = (pin or "").strip()
    if not (pin.isdigit() and 4 <= len(pin) <= 6):
        raise VoicemailError(_("The PIN must be 4 to 6 digits."))
    return pin


# --------------------------------------------------------------------------- mailboxes

def ensure_mailbox(extension: Extension) -> Mailbox:
    """Get or create the mailbox of ``extension`` and make sure Asterisk knows it."""
    mailbox, created = Mailbox.objects.get_or_create(
        extension=extension, defaults={"event": extension.event, "pin": _new_pin()})
    if created:
        sync_realtime(mailbox)
    return mailbox


def update_mailbox(mailbox: Mailbox, actor=None, request=None, **fields) -> Mailbox:
    if "pin" in fields:
        fields["pin"] = validate_pin(fields["pin"])
    changes = {}
    for k, v in fields.items():
        old = getattr(mailbox, k)
        if old != v:
            changes[k] = ["***" if k == "pin" else str(old), "***" if k == "pin" else str(v)]
            setattr(mailbox, k, v)
    if changes:
        mailbox.save()
        audit(action="update", actor=actor, target=mailbox, event=mailbox.event, request=request, changes=changes)
    sync_realtime(mailbox)
    return mailbox


def sync_realtime(mailbox: Mailbox) -> None:
    """Write/refresh the ``voicemail_users`` row Asterisk's ``VoiceMail()`` reads (context ``pet-<slug>``).

    Also mirrors PIN/e-mail into ``Extension.config`` so a PBX resync
    (``AsteriskPBX._sync_voicemail_user``) produces the same row.
    """
    ext = mailbox.extension
    ctx = dp.event_context(mailbox.event)
    if not mailbox.enabled or not enabled("voicemail", mailbox.event):
        VoicemailUser.objects.filter(context=ctx, mailbox=ext.number).delete()
    else:
        VoicemailUser.objects.update_or_create(context=ctx, mailbox=ext.number, defaults={
            "password": mailbox.pin[:80],
            "fullname": ext.caller_id_name[:80],
            "email": (mailbox.delivery_address if mailbox.email_delivery else "")[:80],
            "attach": "yes" if mailbox.email_delivery else "no",
            "attachfmt": "wav" if mailbox.email_delivery else None,
            "language": ext.language or mailbox.event.default_language or None,
            "tz": None, "saycid": "yes", "envelope": "yes", "review": "no",
            "maxmsg": mailbox.max_messages,
            "stamp": datetime.now(UTC),
        })
    cfg = dict(ext.config or {})
    cfg["voicemail_pin"] = mailbox.pin
    cfg["voicemail_email"] = bool(mailbox.email_delivery)
    cfg["voicemail_enabled"] = bool(mailbox.enabled)
    if cfg != (ext.config or {}):
        Extension.objects.filter(pk=ext.pk).update(config=cfg)
        ext.config = cfg


def mailboxes_for(user, event):
    """Mailboxes for the user's active endpoint extensions (created on demand)."""
    exts = Extension.objects.filter(event=event, owner=user, type__in=MAILBOX_TYPES).active().order_by("number")
    return [ensure_mailbox(e) for e in exts]


def can_access(user, mailbox: Mailbox) -> bool:
    return user.is_authenticated and (mailbox.extension.owner_id == user.pk or user.is_orga(mailbox.event))


# --------------------------------------------------------------------------- messages

def _caller_name(event, caller: str) -> str:
    ext = Extension.objects.filter(event=event, number=caller).active().select_related("owner").first()
    return ext.caller_id_name[:80] if ext else ""


def _import_audio(message: Message, file_path: str) -> bool:
    if not file_path or not os.path.isfile(file_path):
        log.warning("voicemail: audio file %r for mailbox %s not found; storing message without audio",
                    file_path, message.mailbox.number)
        return False
    base = os.path.basename(file_path)
    stamp = timezone.localtime(message.received_at).strftime("%Y%m%d-%H%M%S")
    name = f"voicemail/{message.mailbox.event.slug}/{message.mailbox.number}/{stamp}-{base}"
    try:
        with open(file_path, "rb") as fh:
            stored = default_storage.save(name, File(fh, name=base))
    except OSError as exc:
        log.warning("voicemail: could not import %r: %s", file_path, exc)
        return False
    message.audio.name = stored
    if _delete_spool():
        try:
            os.remove(file_path)
        except OSError:
            pass
    return True


def _delete_spool() -> bool:
    """``settings.PET_VOICEMAIL = {"delete_spool": True}`` removes the Asterisk spool file after import."""
    from django.conf import settings

    return bool((getattr(settings, "PET_VOICEMAIL", None) or {}).get("delete_spool", False))


@transaction.atomic
def store_message(event, mailbox_number: str, caller: str, file_path: str, duration: int) -> Message | None:
    """PBX hook entry point (``externnotify``). Returns the stored message or ``None`` if the mailbox is unknown."""
    if not enabled("voicemail", event):
        log.info("voicemail disabled for %s; dropping message for %s", event.slug, mailbox_number)
        return None
    number = (mailbox_number or "").split("@")[0].strip()
    ext = Extension.objects.filter(event=event, number=number).active().select_related("owner").first()
    if ext is None:
        log.warning("voicemail: message for unknown mailbox %s@%s", number, event.slug)
        return None
    mailbox = ensure_mailbox(ext)
    file_path = (file_path or "").strip()[:255]
    if file_path:
        existing = Message.objects.filter(mailbox=mailbox, asterisk_msg_id=file_path).first()
        if existing is not None:
            return existing  # duplicate notify
    caller = (caller or "").strip()
    try:
        duration = max(0, int(duration or 0))
    except (TypeError, ValueError):
        duration = 0
    msg = Message(mailbox=mailbox, caller_number=caller[:32], caller_name=_caller_name(event, caller),
                  duration_seconds=duration, asterisk_msg_id=file_path)
    _import_audio(msg, file_path)
    msg.save()
    _enforce_limit(mailbox)
    update_mwi(mailbox)
    if mailbox.email_delivery and mailbox.delivery_address:
        deliver_email(msg)
    return msg


def _enforce_limit(mailbox: Mailbox) -> int:
    """Drop the oldest *read* messages beyond ``max_messages``. Returns number removed."""
    excess = mailbox.messages.count() - mailbox.max_messages
    if excess <= 0:
        return 0
    n = 0
    for old in mailbox.messages.filter(is_read=True).order_by("received_at")[:excess]:
        _delete_files(old)
        old.delete()
        n += 1
    return n


def _delete_files(message: Message) -> None:
    if message.has_audio:
        try:
            message.audio.delete(save=False)
        except Exception:  # noqa: BLE001 - storage hiccups must not block deleting the row
            log.warning("voicemail: could not delete audio %s", message.audio.name)


def update_mwi(mailbox: Mailbox) -> tuple[int, int]:
    """Push message-waiting state to the PBX and to every DECT handset bound to the extension."""
    new = mailbox.messages.filter(is_read=False).count()
    old = mailbox.messages.filter(is_read=True).count()
    ext = mailbox.extension
    try:
        get_pbx(ext.event).set_mwi(ext, new, old)
    except Exception:  # noqa: BLE001
        log.exception("voicemail: PBX MWI update failed for %s", ext.number)
    for b in ext.bindings.filter(is_active=True).select_related("device"):
        dev = b.device
        if dev.type == "dect" and dev.omm_ppn:
            try:
                get_dect(ext.event).set_mwi(dev.omm_ppn, new)
            except Exception:  # noqa: BLE001
                log.exception("voicemail: DECT MWI update failed for ppn %s", dev.omm_ppn)
    return new, old


def deliver_email(message: Message) -> MessageDelivery:
    mailbox = message.mailbox
    to = mailbox.delivery_address
    subject = _("[%(event)s] New voicemail for %(number)s from %(caller)s") % {
        "event": mailbox.event.name, "number": mailbox.number, "caller": message.caller_number or _("unknown")}
    body = _("You received a new voicemail message.\n\nMailbox: %(number)s\nFrom: %(caller)s %(name)s\n"
             "Received: %(at)s\nDuration: %(dur)s seconds\n") % {
        "number": mailbox.number, "caller": message.caller_number or "?", "name": message.caller_name,
        "at": timezone.localtime(message.received_at).strftime("%Y-%m-%d %H:%M"), "dur": message.duration_seconds}
    mail = EmailMessage(subject, body, default_from_email(), [to])
    if message.has_audio:
        try:
            with message.audio.open("rb") as fh:
                mail.attach(os.path.basename(message.audio.name), fh.read(), message.content_type)
        except OSError as exc:
            log.warning("voicemail: attachment failed: %s", exc)
    try:
        mail.send(fail_silently=False)
        return MessageDelivery.objects.create(message=message, kind="email", recipient=to, ok=True)
    except Exception as exc:  # noqa: BLE001
        log.exception("voicemail: e-mail delivery to %s failed", to)
        return MessageDelivery.objects.create(message=message, kind="email", recipient=to, ok=False, error=str(exc))


def mark_read(message: Message, read: bool = True) -> Message:
    if message.is_read != read:
        message.is_read = read
        message.save(update_fields=["is_read", "updated_at"])
        update_mwi(message.mailbox)
    return message


def delete_message(message: Message, actor=None, request=None) -> None:
    mailbox = message.mailbox
    _delete_files(message)
    message.delete()
    audit(action="delete", actor=actor, target=mailbox, event=mailbox.event, request=request,
          message="Voicemail message deleted")
    update_mwi(mailbox)


def unread_count(mailbox_or_extension) -> int:
    mailbox = mailbox_or_extension
    if isinstance(mailbox_or_extension, Extension):
        mailbox = Mailbox.objects.filter(extension=mailbox_or_extension).first()
        if mailbox is None:
            return 0
    return mailbox.messages.filter(is_read=False).count()


def event_summary(event) -> dict:
    """Privacy-preserving orga overview: counts only."""
    from django.db.models import Count, Q

    boxes = Mailbox.objects.filter(event=event).annotate(
        total=Count("messages"), unread=Count("messages", filter=Q(messages__is_read=False)))
    return {
        "mailboxes": boxes.count(),
        "enabled": boxes.filter(enabled=True).count(),
        "messages": sum(b.total for b in boxes),
        "unread": sum(b.unread for b in boxes),
        "email_delivery": boxes.filter(email_delivery=True).count(),
        "boxes": [{"number": b.number, "total": b.total, "unread": b.unread, "enabled": b.enabled} for b in boxes],
    }
