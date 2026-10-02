from django import forms
from django.utils.translation import gettext_lazy as _

from .models import Mailbox


class MailboxSettingsForm(forms.ModelForm):
    pin = forms.RegexField(regex=r"^\d{4,6}$", label=_("PIN"), max_length=6,
                           error_messages={"invalid": _("The PIN must be 4 to 6 digits.")},
                           widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}))

    class Meta:
        model = Mailbox
        fields = ["enabled", "pin", "email_delivery", "email", "greeting", "max_messages"]

    def clean_greeting(self):
        f = self.cleaned_data.get("greeting")
        if f and hasattr(f, "size"):
            if f.size > 5 * 1024 * 1024:
                raise forms.ValidationError(_("Greeting must be smaller than 5 MB."))
            name = f.name.lower()
            if not name.endswith((".wav", ".mp3", ".ogg", ".gsm")):
                raise forms.ValidationError(_("Upload a .wav, .mp3, .ogg or .gsm file."))
        return f
