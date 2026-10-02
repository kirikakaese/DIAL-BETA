"""Export hook: ``export_event(event) -> {"ivr": {...}}`` (used by the event export/backup tooling)."""
from apps.extensions.models import ExtensionType

from .models import Announcement, FunService, IVRMenu
from .services import dialplan_for


def export_event(event) -> dict:
    anns = Announcement.objects.filter(extension__event=event, extension__type=ExtensionType.ANNOUNCEMENT)
    menus = IVRMenu.objects.filter(extension__event=event, extension__type=ExtensionType.IVR)
    return {"ivr": {
        "announcements": [{"number": a.extension.number, "owner": getattr(a.extension.owner, "username", None),
                           "tts_text": a.tts_text, "language": a.language, "loop": a.loop,
                           "audio": a.audio.name if a.audio else None} for a in anns.select_related("extension")],
        "menus": [{"number": m.extension.number, "owner": getattr(m.extension.owner, "username", None),
                   "prompt_tts": m.prompt_tts, "language": m.language, "timeout": m.timeout,
                   "invalid_retries": m.invalid_retries, "options": m.options,
                   "dialplan": dialplan_for(m.extension)} for m in menus.select_related("extension")],
        "fun_services": [{"kind": f.kind, "number": f.extension.number if f.extension else None}
                         for f in FunService.objects.filter(event=event).select_related("extension")],
    }}
