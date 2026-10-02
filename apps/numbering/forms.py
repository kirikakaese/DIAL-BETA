from django import forms
from django.utils.translation import gettext_lazy as _

from apps.extensions.models import USER_SELECTABLE_TYPES, ExtensionType

from .models import ExtensionClaim, ExtensionPool, NumberPlan


class NumberingSettingsForm(forms.ModelForm):
    """Plan switches that the generic number plan form does not expose."""

    class Meta:
        model = NumberPlan
        fields = ["prefix_free"]


class ExtensionPoolForm(forms.ModelForm):
    class Meta:
        model = ExtensionPool
        fields = ["name", "prefix", "length", "is_active", "description"]

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["prefix"].widget.attrs.update({"inputmode": "numeric", "autocomplete": "off"})
        if event is not None:
            plan = getattr(event, "number_plan", None)
            if plan is not None and not self.instance.pk:
                self.fields["length"].initial = plan.min_length


class ClaimCreateForm(forms.Form):
    number = forms.CharField(label=_("Number"), max_length=16,
                             widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}))
    email = forms.EmailField(
        label=_("E-mail of the person"),
        help_text=_("Existing accounts are matched by address; otherwise the link is mailed as an invite."),
    )
    type = forms.ChoiceField(label=_("Type"), choices=[(t.value, t.label) for t in USER_SELECTABLE_TYPES],
                             initial=ExtensionType.DECT)
    valid_until = forms.DateTimeField(label=_("Valid until"), required=False,
                                      widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
                                      help_text=_("Empty: 14 days."))
    note = forms.CharField(label=_("Note"), max_length=200, required=False)
    send_email = forms.BooleanField(label=_("Send invite e-mail"), required=False, initial=True)

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        if event is not None:
            self.fields["number"].widget.attrs["data-availability-url"] = (
                f"/api/v1/availability/?event={event.slug}&number=")

    def clean_number(self):
        n = self.cleaned_data["number"].strip()
        if not n.isdigit():
            raise forms.ValidationError(_("Digits only."))
        return n


class ClaimAdminForm(forms.ModelForm):
    class Meta:
        model = ExtensionClaim
        fields = ["event", "number", "type", "user", "email", "valid_until", "note"]
