"""IVR services: build the routing payload for the PBX and manage announcements / menus.

Payload contract (consumed by ``apps.pbx.api._ivr_payload``)::

    {"type": "announcement"|"ivr", "greeting": <audio path/URL or "tts:<text>">, "tts": <text>,
     "language": "en", "loop": bool, "timeout": n, "invalid": "pbx-invalid", "retries": n,
     "options": {"1": "4242", "2": "vm:4300", "0": "hangup"}, "actions": [<raw option rows>]}

``options`` maps a digit to what the dialplan should do: an extension number (dial / announcement /
menu targets are all dialed through the event context), ``vm:<box>`` or ``hangup``.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import wave
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.audit import log as audit_log
from apps.core.features import enabled
from apps.events.webhooks import emit
from apps.extensions.models import Extension, ExtensionType
from apps.extensions.services import register

from .models import IVR_ACTIONS, Announcement, IVRMenu

logger = logging.getLogger("dial.ivr")

# ``Extension.config`` key holding the last phone recording (path reference when the wav could not be imported)
PHONE_RECORDING_KEY = "phone_recording"


class IVRError(Exception):
    pass


# --------------------------------------------------------------------------- media helpers

def media_ref(filefield) -> str:
    """Filesystem path if the storage has one (Asterisk reads files directly), else the URL."""
    if not filefield:
        return ""
    try:
        return filefield.path
    except (NotImplementedError, AttributeError, ValueError):
        return filefield.url


def tts_render(text: str, lang: str = "en") -> str | None:
    """Render ``text`` to a wav under ``MEDIA_ROOT/ivr/tts/`` using an external TTS command.

    Pluggable via ``DIAL_TTS_COMMAND`` (e.g. ``"espeak-ng -v {lang} -w {out} {text}"`` or
    ``"piper --model {lang} --output_file {out}"`` reading text from stdin when ``{text}`` is absent).
    Returns the wav path, or ``None`` when no command is configured or rendering failed - the PBX
    then falls back to its own TTS / the ``tts:`` greeting marker.
    """
    cmd = getattr(settings, "DIAL_TTS_COMMAND", None)
    text = (text or "").strip()
    if not cmd or not text:
        return None
    out_dir = Path(settings.MEDIA_ROOT) / "ivr" / "tts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{abs(hash((text, lang)))}.wav"
    if out.exists():
        return str(out)
    argv = [a.format(text=text, lang=lang, out=str(out)) for a in shlex.split(cmd)]
    try:
        subprocess.run(argv, input=None if "{text}" in cmd else text.encode(), check=True, timeout=60,
                       capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("TTS render failed: %s", exc)
        return None
    return str(out) if out.exists() else None


def _greeting(audio, tts_text: str, lang: str, reference: str = "") -> str:
    """Audio file wins, then a phone recording DIAL could not import (played by Asterisk from its own disk),
    then rendered TTS, then the ``tts:`` marker."""
    if audio:
        return media_ref(audio)
    if reference:
        return reference
    rendered = tts_render(tts_text, lang)
    if rendered:
        return rendered
    return f"tts:{tts_text.strip()}" if tts_text.strip() else ""


def _recording_reference(ext) -> str:
    """Asterisk-playable path (without extension) of a phone recording that lives only on the PBX."""
    rec = (ext.config or {}).get(PHONE_RECORDING_KEY) or {}
    return "" if rec.get("imported", True) else str(rec.get("playback") or "")


# --------------------------------------------------------------------------- options

def validate_options(options) -> list[dict]:
    """Normalise ``[{"digit", "action", "target", "label"}]``; raise ``IVRError`` on bad input."""
    if not isinstance(options, list):
        raise IVRError(_("Options must be a list."))
    seen, out = set(), []
    for row in options:
        if not isinstance(row, dict):
            raise IVRError(_("Each option must be an object."))
        digit = str(row.get("digit", "")).strip()
        action = str(row.get("action", "dial")).strip() or "dial"
        target = str(row.get("target", "")).strip()
        if digit not in tuple("0123456789*#"):
            raise IVRError(_("Invalid digit %(d)r.") % {"d": digit})
        if digit in seen:
            raise IVRError(_("Digit %(d)s is used twice.") % {"d": digit})
        if action not in IVR_ACTIONS:
            raise IVRError(_("Unknown action %(a)r.") % {"a": action})
        if action != "hangup" and not target:
            raise IVRError(_("Option %(d)s needs a target.") % {"d": digit})
        seen.add(digit)
        out.append({"digit": digit, "action": action, "target": target, "label": str(row.get("label", ""))[:60]})
    return out


def _option_map(options: list[dict]) -> dict[str, str]:
    out = {}
    for o in options:
        if o["action"] == "hangup":
            out[o["digit"]] = "hangup"
        elif o["action"] == "voicemail":
            out[o["digit"]] = f"vm:{o['target']}"
        else:
            out[o["digit"]] = o["target"]
    return out


# --------------------------------------------------------------------------- PBX contract

def dialplan_for(extension) -> dict:
    """Routing payload for an ``announcement``/``ivr`` extension; ``{}`` when the flag is off or unknown."""
    if extension is None or not enabled("ivr", extension.event):
        return {}
    if extension.type == ExtensionType.ANNOUNCEMENT:
        ann = Announcement.objects.filter(extension=extension).first()
        if ann is None:
            return {}
        return {"type": "announcement",
                "greeting": _greeting(ann.audio, ann.tts_text, ann.language, _recording_reference(extension)),
                "tts": ann.tts_text, "language": ann.language, "loop": ann.loop, "timeout": 0,
                "invalid": "pbx-invalid", "options": {}, "actions": []}
    if extension.type == ExtensionType.IVR:
        menu = IVRMenu.objects.filter(extension=extension).first()
        if menu is None:
            return {}
        opts = validate_options(menu.options or [])
        return {"type": "ivr", "greeting": _greeting(menu.prompt_audio, menu.prompt_tts, menu.language),
                "tts": menu.prompt_tts, "language": menu.language, "loop": False, "timeout": menu.timeout,
                "invalid": "pbx-invalid", "retries": menu.invalid_retries, "options": _option_map(opts),
                "actions": opts}
    return {}


# --------------------------------------------------------------------------- CRUD

def _sync_config(ext, payload: dict):
    """Mirror the payload into ``Extension.config`` so the PBX has a fallback without this app."""
    ext.config = {**(ext.config or {}), "audio": payload.get("greeting", ""), "options": payload.get("options", {}),
                  "timeout": payload.get("timeout", 5), "loop": payload.get("loop", False)}
    ext.save(update_fields=["config", "updated_at"])


# --------------------------------------------------------------------------- recording by phone

def recording_enabled(plan) -> bool:
    return bool((plan.announcement_record_number or "").strip())


def ensure_record_code(ext) -> str:
    """Announcements that predate the field get their code on first use."""
    if not ext.announcement_record_code:
        ext.issue_record_code()
    return ext.announcement_record_code


def record_dial_string(plan, ext) -> str:
    """``<record number><code>`` the owner dials, or ``""`` when the feature is off / not an active announcement."""
    if not recording_enabled(plan) or ext.type != ExtensionType.ANNOUNCEMENT or not ext.is_active:
        return ""
    return f"{plan.announcement_record_number.strip()}{ensure_record_code(ext)}"


def _announcement_for_code(event, code: str):
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if not code:
        return None
    return (Extension.objects.filter(event=event, type=ExtensionType.ANNOUNCEMENT, announcement_record_code=code,
                                     state=Extension.State.ACTIVE).select_related("owner", "event").first())


def _caller_identity(event, caller: str, callerid: str):
    """``(device, extension)`` of the calling phone: PJSIP endpoint name → device; caller ID → active extension.
    At least one of them must resolve for the caller to count as an endpoint of the event."""
    from apps.callback.services import active_extension
    from apps.devices.models import Device

    device = None
    caller = (caller or "").strip()
    if caller:
        device = Device.objects.filter(event=event, sip_username=caller).select_related("owner").first()
    ext = active_extension(event, callerid) or (active_extension(event, caller) if caller.isdigit() else None)
    if ext is None and device is not None:
        binding = device.bindings.filter(is_active=True, extension__event=event,
                                         extension__state=Extension.State.ACTIVE).select_related(
            "extension__owner").order_by("priority").first()
        ext = binding.extension if binding else None
    return device, ext


def _may_record(target, device, caller_ext) -> bool:
    """Owner's device / owner's extension, or a phone of the orga/helpdesk team; ownerless announcements are open
    to every active endpoint of the event."""
    if device is None and caller_ext is None:
        return False
    if target.owner_id is None:
        return True
    users = [u for u in (getattr(device, "owner", None), getattr(caller_ext, "owner", None)) if u is not None]
    for user in users:
        if user.pk == target.owner_id or user.is_helpdesk(target.event):
            return True
    return False


def begin_phone_recording(event, caller: str, code: str, callerid: str = "") -> dict | None:
    """``announcement-record-start`` hook: validate ``code`` and tell Asterisk where to record.

    Returns ``{"handled": True, "number", "name", "file"}`` (``file`` = absolute path without extension under
    ``settings.DIAL_RECORDING_DIR``, ``name`` = its basename) or ``None`` when the code is unknown, the caller is
    not an endpoint of the event or may not record this announcement.
    """
    from apps.extensions.services import get_plan

    if not enabled("ivr", event) or not recording_enabled(get_plan(event)):
        return None
    ext = _announcement_for_code(event, code)
    if ext is None:
        logger.info("record-announcement: unknown code %r in %s", code, event.slug)
        return None
    device, caller_ext = _caller_identity(event, caller, callerid)
    if not _may_record(ext, device, caller_ext):
        logger.info("record-announcement: caller %r/%r may not record %s@%s", caller, callerid, ext.number, event.slug)
        return None
    name = f"{event.slug}-{ext.number}-{timezone.now():%Y%m%d-%H%M%S}"
    return {"handled": True, "number": ext.number, "name": name,
            "file": os.path.join(settings.DIAL_RECORDING_DIR, name)}


def _wav_duration(path: str) -> int | None:
    try:
        with wave.open(path, "rb") as w:
            rate = w.getframerate()
            return round(w.getnframes() / rate) if rate else None
    except (OSError, wave.Error, EOFError):
        return None


def _import_recording(ext, file_path: str) -> str:
    """Copy the PBX file into ``MEDIA_ROOT/ivr/<slug>/<number>/``; returns the stored name or ``""``."""
    if not file_path or not os.path.isfile(file_path):
        return ""
    allowed = os.path.realpath(settings.DIAL_RECORDING_DIR)
    if os.path.commonpath([allowed, os.path.realpath(file_path)]) != allowed:
        logger.warning("record-announcement: refusing to import %r (outside DIAL_RECORDING_DIR)", file_path)
        return ""
    base = os.path.basename(file_path)
    name = f"ivr/{ext.event.slug}/{ext.number}/phone-{base}"
    try:
        with open(file_path, "rb") as fh:
            return default_storage.save(name, File(fh, name=base))
    except OSError as exc:
        logger.warning("record-announcement: could not import %r: %s", file_path, exc)
        return ""


def finish_phone_recording(event, code: str, file_path: str, duration: int = 0) -> Announcement | None:
    """``announcement-recorded`` hook: attach the recording to the announcement identified by ``code``.

    The wav is copied into media storage when DIAL can read ``file_path`` (shared ``recordings`` volume); otherwise
    the path is kept as a playback reference in ``Extension.config["phone_recording"]`` and used as greeting
    until a file is uploaded/imported. ``tts_text`` stays untouched - audio always takes precedence.
    Returns the announcement, or ``None`` for an unknown code.
    """
    from apps.extensions.tasks import provision_extension

    if not enabled("ivr", event):
        return None
    ext = _announcement_for_code(event, code)
    if ext is None:
        logger.info("record-announcement: finish with unknown code %r in %s", code, event.slug)
        return None
    file_path = (file_path or "").strip()
    try:
        duration = max(0, int(duration or 0))
    except (TypeError, ValueError):
        duration = 0
    stored = _import_recording(ext, file_path)
    if stored:
        duration = _wav_duration(file_path) or duration
    playback = os.path.splitext(file_path)[0]
    rec = {"file": file_path[:255], "playback": playback[:255], "duration": duration, "imported": bool(stored),
           "recorded_at": timezone.now().isoformat()}
    ext.config = {**(ext.config or {}), PHONE_RECORDING_KEY: rec}
    ext.save(update_fields=["config", "updated_at"])
    ann, _created = Announcement.objects.get_or_create(extension=ext)
    if stored:
        ann.audio = stored
        ann.save(update_fields=["audio", "updated_at"])
    _sync_config(ext, dialplan_for(ext))
    audit_log(action="update", actor=ext.owner, target=ext, event=event,
              message="Announcement recorded by phone" if stored else
              "Announcement recorded by phone (file kept on the PBX, not imported)",
              changes={"audio": [None, stored or None], PHONE_RECORDING_KEY: rec})
    emit("announcement.recorded", {"extension": ext.number, "event": event.slug, "audio": stored or None,
                                   "file": file_path, "duration": duration, "imported": bool(stored),
                                   "announcement": ann.pk}, event=event)
    provision_extension.delay(str(ext.pk))
    return ann


def create_announcement(event, owner, number: str, tts_text: str = "", audio=None, *, language="en", loop=False,
                        request=None, **ext_fields) -> Announcement:
    if not enabled("ivr", event):
        raise IVRError(_("IVR is disabled for this event."))
    if not (tts_text or audio):
        raise IVRError(_("Provide TTS text or an audio file."))
    ext = register(event, owner, number, ExtensionType.ANNOUNCEMENT, request=request, **ext_fields)
    ann = Announcement.objects.create(extension=ext, tts_text=tts_text or "", audio=audio, language=language,
                                      loop=loop)
    _sync_config(ext, dialplan_for(ext))
    return ann


def update_announcement(ann: Announcement, **fields) -> Announcement:
    for k, v in fields.items():
        setattr(ann, k, v)
    ann.save()
    _sync_config(ann.extension, dialplan_for(ann.extension))
    return ann


def create_menu(event, owner, number: str, options, *, prompt_tts="", prompt_audio=None, language="en",
                timeout=5, invalid_retries=3, request=None, **ext_fields) -> IVRMenu:
    if not enabled("ivr", event):
        raise IVRError(_("IVR is disabled for this event."))
    opts = validate_options(options)
    ext = register(event, owner, number, ExtensionType.IVR, request=request, **ext_fields)
    menu = IVRMenu.objects.create(extension=ext, prompt_tts=prompt_tts, prompt_audio=prompt_audio, language=language,
                                  timeout=timeout, invalid_retries=invalid_retries, options=opts)
    _sync_config(ext, dialplan_for(ext))
    return menu


def update_menu(menu: IVRMenu, **fields) -> IVRMenu:
    if "options" in fields:
        fields["options"] = validate_options(fields["options"])
    for k, v in fields.items():
        setattr(menu, k, v)
    menu.save()
    _sync_config(menu.extension, dialplan_for(menu.extension))
    return menu
