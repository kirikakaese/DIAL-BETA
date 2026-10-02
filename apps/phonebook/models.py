"""Phonebook: per-event presentation settings.

The entries themselves are ``extensions.Extension`` rows (``in_phonebook``, ``display_name``,
``location_hint``, ``type``, ``owner``); this app only adds how they are shown/exported.
"""
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


def _directory_token() -> str:
    import secrets

    return secrets.token_urlsafe(24)


class PhonebookSettings(TimeStampedModel):
    event = models.OneToOneField("events.Event", on_delete=models.CASCADE, related_name="phonebook_settings")
    intro_text = models.TextField(blank=True, help_text=_("Shown above the phonebook (also on the PDF)."))
    show_location = models.BooleanField(default=True, verbose_name=_("Show location hints"))
    show_owner = models.BooleanField(default=True, verbose_name=_("Show owner nicknames"))
    # Optional grouping: [{"name": "Orga", "prefix": "1"}, {"name": "Angels", "prefix": "2"}]
    categories = models.JSONField(default=list, blank=True,
                                  help_text=_("List of {name, prefix} objects used to group entries."))
    # Remote directory for desk phones / OMM / LDAP (feature #9): phones cannot log in, so they present
    # this event-wide secret instead (URL path segment for XML, bind password for LDAP).
    directory_token = models.CharField(_("Directory token"), max_length=64, default=_directory_token,
                                       help_text=_("Secret in remote-phonebook URLs and the LDAP bind password. "
                                                   "Rotate it to lock out every phone at once."))
    directory_enabled = models.BooleanField(_("Remote directory enabled"), default=True,
                                            help_text=_("Serve the phonebook to desk phones, the DECT OMM and "
                                                        "LDAP clients."))

    class Meta:
        verbose_name = _("phonebook settings")
        verbose_name_plural = _("phonebook settings")

    def __str__(self):
        return f"Phonebook settings for {self.event.slug}"

    def category_for(self, number: str) -> str:
        best = ""
        best_len = -1
        for cat in self.categories or []:
            prefix = str(cat.get("prefix", ""))
            if number.startswith(prefix) and len(prefix) > best_len:
                best, best_len = str(cat.get("name", "")), len(prefix)
        return best

    def rotate_directory_token(self) -> str:
        self.directory_token = _directory_token()
        self.save(update_fields=["directory_token", "updated_at"])
        return self.directory_token
