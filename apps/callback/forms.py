"""Portal forms for the callback app."""
from django import forms
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.extensions.models import Extension

from .models import CallbackRequest, ScheduledCall
from .services import event_tz


class ExtensionChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, ext):
        name = ext.display_name or ext.get_type_display()
        return f"{ext.number} – {name}"


class TestRingbackForm(forms.Form):
    extension = ExtensionChoiceField(queryset=Extension.objects.none(), label=_("Extension"))
    delay = forms.IntegerField(min_value=0, max_value=600, label=_("Call me back in (seconds)"))

    def __init__(self, *args, extensions, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["extension"].queryset = extensions
        self.fields["delay"].initial = getattr(settings, "PET_TEST_RINGBACK_DELAY_SECONDS", 10)


class CallbackRequestForm(forms.Form):
    requester = ExtensionChoiceField(queryset=Extension.objects.none(), label=_("Call back to"))
    target_number = forms.CharField(max_length=16, label=_("Extension to reach"),
                                    widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}))
    kind = forms.ChoiceField(choices=CallbackRequest.Kind.choices, initial=CallbackRequest.Kind.CCBS,
                             label=_("Kind"))

    def __init__(self, *args, extensions, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["requester"].queryset = extensions

    def clean_target_number(self):
        v = (self.cleaned_data["target_number"] or "").strip()
        if not v.isdigit():
            raise forms.ValidationError(_("Digits only."))
        return v


class WakeupForm(forms.ModelForm):
    """Times are entered and displayed in the event's timezone."""

    extension = ExtensionChoiceField(queryset=Extension.objects.none(), label=_("Extension"))
    scheduled_for = forms.DateTimeField(
        label=_("When"), widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"],
    )

    class Meta:
        model = ScheduledCall
        fields = ["extension", "scheduled_for", "repeat", "announcement", "announcement_text",
                  "announcement_file", "max_retries", "retry_interval_minutes", "snooze_minutes"]
        labels = {
            "repeat": _("Repeat"), "announcement": _("Announcement"), "announcement_text": _("Announcement text"),
            "announcement_file": _("Announcement audio file"), "max_retries": _("Retries if unanswered"),
            "retry_interval_minutes": _("Retry interval (minutes)"), "snooze_minutes": _("Snooze (minutes)"),
        }

    def __init__(self, *args, extensions, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event
        self.tz = event_tz(event)
        self.fields["extension"].queryset = extensions
        self.fields["scheduled_for"].help_text = _("Event time zone: %(tz)s") % {"tz": self.tz.key}

    def clean_scheduled_for(self):
        value = self.cleaned_data["scheduled_for"]
        # Django made the value aware in the *current* timezone; reinterpret the wall-clock time in the event tz.
        if timezone.is_aware(value):
            value = timezone.make_naive(value, timezone.get_current_timezone())
        value = timezone.make_aware(value, self.tz)
        if value <= timezone.now():
            raise forms.ValidationError(_("The time must be in the future."))
        return value

    def clean(self):
        data = super().clean()
        if data.get("announcement") == ScheduledCall.Announcement.CUSTOM and not (
                data.get("announcement_text") or data.get("announcement_file")):
            self.add_error("announcement_text", _("Enter a text or upload a file for a custom announcement."))
        return data
