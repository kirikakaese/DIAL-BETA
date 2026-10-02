"""Orga forms for the per-event venue infrastructure (PBX + DECT connection)."""
from django import forms
from django.utils.translation import gettext_lazy as _

from apps.dect.models import DECTConnection, dect_backend_choices
from apps.portal.forms import PetModelForm

from .models import PBXConnection, pbx_backend_choices


class _SecretFieldsMixin:
    """Password fields are never rendered back; an empty submission keeps the stored value."""

    secret_fields: tuple[str, ...] = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.secret_fields:
            f = self.fields[name]
            f.required = False
            f.widget = forms.PasswordInput(attrs={"autocomplete": "new-password"})
            if self.instance and self.instance.pk and getattr(self.instance, name):
                f.help_text = _("Stored. Leave empty to keep the current value.")

    def clean(self):
        data = super().clean()
        for name in self.secret_fields:
            if not data.get(name) and self.instance and self.instance.pk:
                data[name] = getattr(self.instance, name)
        return data


class PBXConnectionForm(_SecretFieldsMixin, PetModelForm):
    secret_fields = ("ari_password", "ami_password", "hook_secret")
    backend = forms.ChoiceField(label=_("Backend"), choices=pbx_backend_choices)

    class Meta:
        model = PBXConnection
        fields = ["backend", "provisioning", "agent_poll_interval", "ari_url", "ari_user", "ari_password",
                  "ari_app", "ami_host", "ami_port", "ami_user", "ami_password", "hook_secret", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # optional in submissions (older API clients / scripts post without them) - defaults apply in clean()
        self.fields["provisioning"].required = False
        self.fields["agent_poll_interval"].required = False

    def clean(self):
        data = super().clean()
        if not data.get("provisioning"):
            data["provisioning"] = PBXConnection.Provisioning.SHARED_DB
        if not data.get("agent_poll_interval"):
            data["agent_poll_interval"] = PBXConnection._meta.get_field("agent_poll_interval").default
        agent = data.get("provisioning") == PBXConnection.Provisioning.AGENT
        # in agent mode the venue box is usually not reachable from PET - ARI is optional there
        if data.get("backend") == "asterisk" and not data.get("ari_url") and not agent:
            self.add_error("ari_url", _("The ARI URL is required for Asterisk."))
        return data


class DECTConnectionForm(_SecretFieldsMixin, PetModelForm):
    secret_fields = ("password",)
    backend = forms.ChoiceField(label=_("Backend"), choices=dect_backend_choices)

    class Meta:
        model = DECTConnection
        fields = ["backend", "host", "port", "user", "password", "verify_tls", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def clean(self):
        data = super().clean()
        if data.get("backend") == "omm" and not data.get("host"):
            self.add_error("host", _("The OMM host is required."))
        return data
