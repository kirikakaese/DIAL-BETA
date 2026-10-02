"""Event export hook: ``apps.events.export.export_event`` merges this dict.

The ``directory_token`` is deliberately left out: an exported / cloned event must get a fresh secret.
"""
from .models import PhonebookSettings


def export_event(event) -> dict:
    s = PhonebookSettings.objects.filter(event=event).first()
    if s is None:
        return {"phonebook_settings": None}
    return {"phonebook_settings": {
        "intro_text": s.intro_text, "show_location": s.show_location, "show_owner": s.show_owner,
        "categories": s.categories, "directory_enabled": s.directory_enabled,
    }}
