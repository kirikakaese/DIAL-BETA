from django import forms
from django.utils.translation import gettext_lazy as _

from .models import emc_validator


class VendorForm(forms.Form):
    """Orga: add or rename a DECT manufacturer (EMC -> vendor)."""

    emc = forms.CharField(label="EMC", max_length=5, validators=[emc_validator],
                          widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}),
                          help_text=_("First 5 digits of the IPEI."))
    name = forms.CharField(label=_("Vendor"), max_length=80)
    models_hint = forms.CharField(label=_("Known models"), max_length=500, required=False,
                                  help_text=_("Optional, e.g. '612d, 622d'."))


class SuggestVendorForm(forms.Form):
    """User: tell us who made a handset with an unknown EMC."""

    name = forms.CharField(label=_("Vendor"), max_length=80)


class GSMDeviceForm(forms.Form):
    """Orga: register a GSM handset for an extension."""

    number = forms.CharField(label=_("Extension number"), max_length=16, required=False,
                             help_text=_("Active extension in this event the SIM should ring; may be left empty."),
                             widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}))
    name = forms.CharField(label=_("Device name"), max_length=80, required=False)
    msisdn = forms.CharField(label="MSISDN", max_length=20, required=False,
                             help_text=_("Number of the SIM in the GSM core, if already known."))
    imsi = forms.CharField(label="IMSI", max_length=15, required=False)
