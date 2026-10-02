from django import forms
from django.utils.translation import gettext_lazy as _

from apps.events.models import UserGroup

from .models import CallGroup


class CreateGroupForm(forms.Form):
    number = forms.CharField(label=_("Number"), max_length=16)
    name = forms.CharField(label=_("Group name"), max_length=60)
    shortcode = forms.CharField(label=_("Shortcode"), max_length=8, required=False,
                                help_text=_("Short label (e.g. SEC) shown before the caller name on ringing handsets."))
    strategy = forms.ChoiceField(label=_("Ring strategy"), choices=CallGroup.Strategy.choices,
                                 initial=CallGroup.Strategy.RING_ALL)
    description = forms.CharField(label=_("Description"), max_length=200, required=False)
    ring_timeout = forms.IntegerField(label=_("Ring timeout (s)"), min_value=5, max_value=120, initial=20)
    wrap_up_seconds = forms.IntegerField(label=_("Wrap-up time (s)"), min_value=0, max_value=3600, initial=0)
    allow_self_service = forms.BooleanField(label=_("Members may log in/out themselves"), required=False,
                                            initial=True)
    user_group = forms.ModelChoiceField(label=_("Auto-join user group"), queryset=UserGroup.objects.none(),
                                        required=False)
    in_phonebook = forms.BooleanField(label=_("List in phonebook"), required=False, initial=True)

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["number"].widget.attrs.update({
            "inputmode": "numeric", "autocomplete": "off",
            "data-availability-url": f"/api/v1/availability/?event={event.slug}&number=" if event else "",
        })
        if event is not None:
            self.fields["user_group"].queryset = UserGroup.objects.filter(event=event)

    def clean_number(self):
        n = self.cleaned_data["number"].strip()
        if not n.isdigit():
            raise forms.ValidationError(_("Digits only."))
        return n

    def clean_shortcode(self):
        return (self.cleaned_data.get("shortcode") or "").strip()


class GroupSettingsForm(forms.ModelForm):
    name = forms.CharField(label=_("Group name"), max_length=60)

    class Meta:
        model = CallGroup
        fields = ["strategy", "ring_timeout", "wrap_up_seconds", "allow_self_service", "description", "shortcode",
                  "user_group"]

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user_group"].queryset = (
            UserGroup.objects.filter(event=event) if event else UserGroup.objects.none())
        if self.instance and self.instance.pk:
            self.fields["name"].initial = self.instance.extension.display_name


class AddMemberForm(forms.Form):
    number = forms.CharField(label=_("Extension number"), max_length=16)
    priority = forms.IntegerField(label=_("Priority"), min_value=0, max_value=99, initial=0, required=False)
    delay_s = forms.IntegerField(label=_("Ring delay (s)"), min_value=0, max_value=120, initial=0, required=False)


class MemberSettingsForm(forms.Form):
    priority = forms.IntegerField(label=_("Priority"), min_value=0, max_value=99, required=False)
    delay_s = forms.IntegerField(label=_("Ring delay (s)"), min_value=0, max_value=120, required=False)


class InviteForm(forms.Form):
    number = forms.CharField(label=_("Extension number"), max_length=16)
    reason = forms.CharField(label=_("Reason"), max_length=200, required=False,
                             widget=forms.TextInput(attrs={"placeholder": _("Why should they join? (optional)")}))


class AdminForm(forms.Form):
    identifier = forms.CharField(label=_("E-mail or nickname"), max_length=254)
