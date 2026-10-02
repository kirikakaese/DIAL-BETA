"""Forms for the self-service and orga portal."""
from __future__ import annotations

import datetime
import re

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import User
from apps.core import features
from apps.devices import endpoint_types
from apps.devices.models import Device, DeviceType, ipei_validator
from apps.events.models import SCHEDULE_FIELDS, Event, EventMembership, UserGroup, Webhook, validate_schedule
from apps.extensions.audio import validate_ringback_upload
from apps.extensions.models import ENDPOINT_TYPES, USER_SELECTABLE_TYPES, Extension, ExtensionType
from apps.extensions.services import ExtensionError, validate_device_binding
from apps.numbering.models import NumberPlan, NumberRange

# ----------------------------------------------------------------------------- helpers


class AccessibleFormMixin:
    """Adds ``aria-describedby`` / ``aria-invalid`` so the ``_form.html`` include is screen-reader friendly."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if field.help_text:
                field.widget.attrs.setdefault("aria-describedby", f"{self.auto_id % name}_help")

    def full_clean(self):
        super().full_clean()
        if not self.is_bound:
            return
        for name in self.errors:
            if name in self.fields:
                w = self.fields[name].widget
                w.attrs["aria-invalid"] = "true"
                desc = w.attrs.get("aria-describedby", "")
                w.attrs["aria-describedby"] = f"{desc} {self.auto_id % name}_errors".strip()


class PetForm(AccessibleFormMixin, forms.Form):
    pass


class PetModelForm(AccessibleFormMixin, forms.ModelForm):
    pass


class CommaListField(forms.CharField):
    """Comma/whitespace separated list of tokens <-> JSON list."""

    def __init__(self, *args, digits_only=False, **kwargs):
        kwargs.setdefault("required", False)
        self.digits_only = digits_only
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        if isinstance(value, (list, tuple)):
            return ", ".join(str(v) for v in value)
        return value

    def to_python(self, value):
        if isinstance(value, (list, tuple)):
            return list(value)
        raw = super().to_python(value) or ""
        items = [t for t in re.split(r"[\s,;]+", raw) if t]
        if self.digits_only:
            for t in items:
                if not t.isdigit():
                    raise forms.ValidationError(_("'%(t)s' is not a number.") % {"t": t})
        return items


class EventLocalDateTimeField(forms.DateTimeField):
    """``datetime-local`` input whose naive value is wall-clock time in ``tz`` (the event's timezone).

    Set ``field.tz`` after construction (e.g. in the form's ``__init__``); ``None`` means the server timezone.
    """

    input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"]
    widget = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")
    tz = None

    def prepare_value(self, value):
        if isinstance(value, datetime.datetime) and timezone.is_aware(value):
            return timezone.localtime(value, self.tz).replace(tzinfo=None)
        return value

    def to_python(self, value):
        with timezone.override(self.tz):
            return super().to_python(value)


def find_user(identifier: str) -> User | None:
    ident = (identifier or "").strip()
    if not ident:
        return None
    return User.objects.filter(email__iexact=ident).first() or User.objects.filter(username__iexact=ident).first()


def selectable_extension_types(event):
    """Extension types an end user may register in ``event`` (endpoint types need an enabled device class)."""
    enabled_devices = {t.key for t in endpoint_types.all_types_for(event)}
    feature_for = {
        ExtensionType.GROUP: "callgroups", ExtensionType.ANNOUNCEMENT: "ivr", ExtensionType.IVR: "ivr",
        ExtensionType.CONFERENCE: "conferences",
    }
    out = []
    for t in USER_SELECTABLE_TYPES:
        if t in ENDPOINT_TYPES and t not in enabled_devices:
            continue
        if t == ExtensionType.TRUNK and DeviceType.SIP not in enabled_devices:
            continue
        flag = feature_for.get(t)
        if flag and not features.enabled(flag, event):
            continue
        out.append((t, ExtensionType(t).label))
    return out


def compatible_endpoint_types(extension: Extension):
    """Device classes that can be bound to ``extension`` (same key as the extension type; a trunk takes exactly
    one SIP account)."""
    key = DeviceType.SIP if extension.is_trunk else extension.type
    return [t for t in endpoint_types.all_types_for(extension.event) if t.key == key]


# ----------------------------------------------------------------------------- user forms


class EventJoinForm(PetForm):
    join_code = forms.CharField(label=_("Group join code"), required=False, max_length=32,
                                help_text=_("Optional: code handed out by your team to join its user group."))


class ExtensionCreateForm(PetForm):
    BLOCK_CHOICES = [("1", _("10 numbers (base ends in 0, e.g. 4710)")),
                     ("2", _("100 numbers (base ends in 00, e.g. 4700)")),
                     ("3", _("1000 numbers (base ends in 000, e.g. 4000)"))]

    number = forms.CharField(label=_("Extension number"), max_length=16,
                             widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off",
                                                           "pattern": "[0-9]*"}))
    type = forms.ChoiceField(label=_("Type"))
    block_digits = forms.TypedChoiceField(
        label=_("Block size"), choices=BLOCK_CHOICES, coerce=int, required=False, initial="2",
        help_text=_("SIP trunks only: your PBX registers once and receives every call to a number of the block "
                    "(e.g. 4700–4799). The number above is the first number of the block."),
    )
    display_name = forms.CharField(label=_("Display name"), max_length=60, required=False,
                                   help_text=_("Shown as caller ID; defaults to your nickname."))
    description = forms.CharField(label=_("Description"), max_length=200, required=False)
    location_hint = forms.CharField(label=_("Location"), max_length=120, required=False,
                                    help_text=_("e.g. 'Hackcenter, table 12'"))
    in_phonebook = forms.BooleanField(label=_("List in the public phonebook"), required=False, initial=True)
    request_note = forms.CharField(label=_("Note for the moderators"), required=False, widget=forms.Textarea(
        attrs={"rows": 3}), help_text=_("Only needed when the number requires approval."))

    def __init__(self, *args, event, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event
        self.fields["type"].choices = selectable_extension_types(event)
        self.fields["number"].widget.attrs.update({
            "data-availability-url": f"/api/v1/availability/?event={event.slug}&number=",
            "data-availability-type": "#id_type",
            "data-availability-block": "#id_block_digits",
        })
        self.fields["block_digits"].widget.attrs["data-only-for-type"] = ExtensionType.TRUNK
        if not any(t == ExtensionType.TRUNK for t, _label in self.fields["type"].choices):
            del self.fields["block_digits"]

    def clean_number(self):
        n = self.cleaned_data["number"].strip()
        if not n.isdigit():
            raise forms.ValidationError(_("Extension must consist of digits only."))
        return n

    def clean(self):
        data = super().clean()
        if data.get("type") == ExtensionType.TRUNK and not data.get("block_digits"):
            self.add_error("block_digits", _("Choose the block size of the trunk (10, 100 or 1000 numbers)."))
        return data

    @property
    def config(self) -> dict:
        """Type-specific ``Extension.config`` for :func:`apps.extensions.services.register`."""
        d = self.cleaned_data
        if d.get("type") == ExtensionType.TRUNK:
            return {"block_digits": int(d["block_digits"])}
        return {}


class ExtensionEditForm(PetModelForm):
    forward_target_number = forms.CharField(
        label=_("…or type a number"), max_length=16, required=False,
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}),
        help_text=_("Alternative to the list above: any active extension of this event."),
    )

    class Meta:
        model = Extension
        fields = ["display_name", "description", "location_hint", "in_phonebook", "ring_strategy", "ring_timeout",
                  "forward_mode", "forward_target", "forward_delay",
                  "forward_unconditional", "forward_busy", "forward_noanswer", "allow_callback", "language",
                  "call_waiting", "display_mode", "dect_encryption", "ringback_tone"]
        labels = {
            "forward_mode": _("Call forwarding"), "forward_target": _("Forward to"),
            "forward_delay": _("Forward after (seconds)"),
            "forward_unconditional": _("Forward all calls to (number)"),
            "forward_busy": _("Forward when busy to (number)"),
            "forward_noanswer": _("Forward when unanswered to (number)"), "ring_timeout": _("Ring timeout (seconds)"),
            "language": _("Announcement language"), "call_waiting": _("Call waiting"),
            "display_mode": _("Caller-ID display"), "dect_encryption": _("DECT encryption"),
            "ringback_tone": _("Custom ringback tone"),
        }
        help_texts = {
            "forward_unconditional": _("Legacy free-text targets; only used while call forwarding is off."),
            "ringback_tone": _("WAV, MP3, OGG or FLAC, max. 5 MB. Callers hear this instead of the ring tone."),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        ext = self.instance
        self.user = user
        self.fields["language"].required = False
        for f in ("forward_unconditional", "forward_busy", "forward_noanswer"):
            self.fields[f].widget.attrs["inputmode"] = "numeric"
        # forwarding: pick from the extensions the user may forward to (orga: everything active in the event)
        qs = Extension.objects.filter(event_id=ext.event_id, state=Extension.State.ACTIVE).exclude(pk=ext.pk)
        is_orga = bool(user is not None and (user.is_superuser or user.is_orga(ext.event)))
        if not is_orga:
            qs = qs.filter(owner=user) if user is not None else qs.none()
        self.is_orga = is_orga
        if ext.forward_target_id and not qs.filter(pk=ext.forward_target_id).exists():
            qs = Extension.objects.filter(pk__in=[*qs.values_list("pk", flat=True), ext.forward_target_id])
        tf = self.fields["forward_target"]
        tf.queryset = qs.order_by("number")
        tf.required = False
        tf.empty_label = _("– choose –")
        tf.label_from_instance = lambda e: f"{e.number} · {e.caller_id_name}"
        for name in ("forward_mode", "display_mode", "forward_delay"):
            self.fields[name].required = False  # absent in POST -> keep current value
        if ext.type != ExtensionType.DECT:
            self.fields.pop("dect_encryption")
        if not ext.is_endpoint:
            for f in ("ringback_tone", "call_waiting", "forward_mode", "forward_target", "forward_delay",
                      "forward_target_number"):
                self.fields.pop(f, None)

    def _clean_forward(self, name):
        v = (self.cleaned_data.get(name) or "").strip()
        if v and not v.isdigit():
            raise forms.ValidationError(_("Digits only."))
        return v

    def clean_forward_unconditional(self):
        return self._clean_forward("forward_unconditional")

    def clean_forward_busy(self):
        return self._clean_forward("forward_busy")

    def clean_forward_noanswer(self):
        return self._clean_forward("forward_noanswer")

    def clean_forward_mode(self):
        return self.cleaned_data.get("forward_mode") or self.instance.forward_mode or Extension.ForwardMode.OFF

    def clean_display_mode(self):
        return self.cleaned_data.get("display_mode") or self.instance.display_mode

    def clean_forward_delay(self):
        v = self.cleaned_data.get("forward_delay")
        if v is None:
            return self.instance.forward_delay or 10
        if not 1 <= v <= 120:
            raise forms.ValidationError(_("Between 1 and 120 seconds."))
        return v

    def clean_ringback_tone(self):
        f = self.cleaned_data.get("ringback_tone")
        if isinstance(f, UploadedFile):
            validate_ringback_upload(f)
        return f

    def clean(self):
        from apps.extensions import services

        data = super().clean()
        if "forward_mode" not in self.fields:
            return data
        mode = data.get("forward_mode") or Extension.ForwardMode.OFF
        target = data.get("forward_target")
        typed = (data.get("forward_target_number") or "").strip()
        if typed:
            if not typed.isdigit():
                self.add_error("forward_target_number", _("Digits only."))
                return data
            resolved = services.resolve_forward_target(self.instance.event, typed)
            if resolved is None:
                self.add_error("forward_target_number", _("No active extension with that number in this event."))
                return data
            target = resolved
        if mode != Extension.ForwardMode.OFF and target is None:
            self.add_error("forward_target", _("Please choose where to forward to."))
            return data
        if target is not None:
            try:
                services.validate_forward_target(self.instance, target)
            except services.ExtensionError as exc:
                self.add_error("forward_target", str(exc))
                return data
        data["forward_target"] = target
        data["forward_mode"] = mode
        return data


class TransferForm(PetForm):
    recipient = forms.CharField(label=_("Recipient"), max_length=254,
                                help_text=_("Nickname or e-mail address of the new owner."))

    def clean_recipient(self):
        u = find_user(self.cleaned_data["recipient"])
        if u is None:
            raise forms.ValidationError(_("No user with that nickname or e-mail address."))
        return u


class DeviceAddForm(PetForm):
    """Add a device to an extension: either a brand-new one or an existing device the user owns."""

    FIELD_LABELS = {
        "ipei": _("IPEI"), "handset_model": _("Handset model"), "name": _("Device name"),
        "sip_transport": _("SIP transport"), "mac_address": _("MAC address"), "imsi": _("IMSI"),
        "msisdn": _("MSISDN"),
    }

    endpoint_type = forms.ChoiceField(label=_("Device class"))
    existing_device = forms.ModelChoiceField(label=_("Attach one of my existing devices"), required=False,
                                             queryset=Device.objects.none(), empty_label=_("— create a new device —"))

    def __init__(self, *args, extension, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.extension = extension
        self.types = compatible_endpoint_types(extension)
        self.fields["endpoint_type"].choices = [(t.key, t.label) for t in self.types]
        if len(self.types) == 1:
            self.fields["endpoint_type"].widget = forms.HiddenInput()
            self.fields["endpoint_type"].initial = self.types[0].key
        bound_ids = extension.bindings.values_list("device_id", flat=True)
        self.fields["existing_device"].queryset = Device.objects.filter(
            event=extension.event, owner=user, type__in=[t.key for t in self.types]).exclude(pk__in=bound_ids)
        if not self.fields["existing_device"].queryset.exists():
            del self.fields["existing_device"]
        self.user_fields: list[str] = []
        for t in self.types:
            for f in t.user_fields:
                if f not in self.user_fields:
                    self.user_fields.append(f)
        for f in self.user_fields:
            self.fields[f] = self._make_field(f)

    def _make_field(self, name):
        label = self.FIELD_LABELS.get(name, name)
        if name == "ipei":
            return forms.CharField(label=label, required=False, max_length=13, validators=[ipei_validator],
                                   widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}),
                                   help_text=_("13 digits, printed under the battery or shown in the handset menu."))
        if name == "sip_transport":
            return forms.ChoiceField(label=label, choices=Device._meta.get_field("sip_transport").choices,
                                     initial="udp")
        if name == "mac_address":
            return forms.CharField(label=label, required=False, max_length=17,
                                   help_text=_("Optional; only needed for autoprovisioning of hard phones."))
        if name == "name":
            return forms.CharField(label=label, required=False, max_length=80,
                                   help_text=_("Free-form label, e.g. 'my orange handset'."))
        return forms.CharField(label=label, required=False, max_length=60)

    def clean_ipei(self):
        v = (self.cleaned_data.get("ipei") or "").strip().replace(" ", "")
        return v

    def clean_mac_address(self):
        v = (self.cleaned_data.get("mac_address") or "").strip().lower()
        if v and not re.fullmatch(r"([0-9a-f]{2}[:-]?){5}[0-9a-f]{2}", v):
            raise forms.ValidationError(_("Enter a MAC address like 00:11:22:aa:bb:cc."))
        return v

    def clean(self):
        data = super().clean()
        if data.get("existing_device"):
            try:
                validate_device_binding(self.extension, data["existing_device"].type, device=data["existing_device"])
            except ExtensionError as exc:
                self.add_error("existing_device", str(exc))
            return data
        et = data.get("endpoint_type")
        if et:
            try:
                validate_device_binding(self.extension, et)
            except ExtensionError as exc:
                self.add_error(None, str(exc))
        if et == DeviceType.DECT and not data.get("ipei"):
            self.add_error("ipei", _("The IPEI is required to subscribe a DECT handset."))
        if et == DeviceType.DECT and data.get("ipei"):
            if Device.objects.filter(event=self.extension.event, ipei=data["ipei"]).exists():
                self.add_error("ipei", _("A handset with this IPEI is already registered in this event. "
                                         "Attach the existing device instead."))
        return data


class DeviceRenameForm(PetModelForm):
    class Meta:
        model = Device
        fields = ["name", "handset_model", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.type != DeviceType.DECT:
            del self.fields["handset_model"]


# ----------------------------------------------------------------------------- orga forms


class EventCreateForm(PetModelForm):
    class Meta:
        model = Event
        fields = ["name", "slug", "start_date", "end_date", "location", "timezone", "is_public"]
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}),
                   "end_date": forms.DateInput(attrs={"type": "date"})}

    def clean(self):
        data = super().clean()
        if data.get("start_date") and data.get("end_date") and data["end_date"] < data["start_date"]:
            self.add_error("end_date", _("End date must not be before the start date."))
        return data


class EventSettingsForm(PetModelForm):
    disabled_features = forms.MultipleChoiceField(
        label=_("Disabled features for this event"), required=False, widget=forms.CheckboxSelectMultiple(
            attrs={"class": "checkbox-list"}),
        help_text=_("Features enabled on this PET instance can be switched off per event."))

    class Meta:
        model = Event
        fields = ["name", "description", "start_date", "end_date", "location", "timezone", "is_public",
                  "logo", "primary_color", "accent_color", "announcement", "sip_domain", "dial_prefix",
                  "default_language", "max_extensions_per_user", "allow_guest_extensions", "allow_breakout",
                  "has_gsm", "gsm_trunk",
                  "cdr_aggregate_only", "cdr_retention_days",
                  "registration_opens_at", "goes_live_at", "archives_at"]
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}),
                   "end_date": forms.DateInput(attrs={"type": "date"}),
                   "primary_color": forms.TextInput(attrs={"type": "color"}),
                   "accent_color": forms.TextInput(attrs={"type": "color"}),
                   "description": forms.Textarea(attrs={"rows": 3}), "announcement": forms.Textarea(attrs={"rows": 3})}
        field_classes = {"registration_opens_at": EventLocalDateTimeField, "goes_live_at": EventLocalDateTimeField,
                         "archives_at": EventLocalDateTimeField}

    SCHEDULE_FIELD_NAMES = tuple(SCHEDULE_FIELDS.values())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["disabled_features"].choices = [(f, f) for f in settings.PET_ALL_FEATURES]
        self.fields["disabled_features"].initial = (self.instance.settings or {}).get("disabled_features", [])
        # Schedule inputs are wall-clock times in the *event's* timezone, not the server's.
        for name in self.SCHEDULE_FIELD_NAMES:
            self.fields[name].tz = self.instance.tzinfo

    def general_fields(self):
        return [f for f in self.visible_fields() if f.name not in self.SCHEDULE_FIELD_NAMES]

    def schedule_fields(self):
        return [self[name] for name in self.SCHEDULE_FIELD_NAMES]

    def clean(self):
        data = super().clean()
        try:
            validate_schedule(self.instance.state, data.get("registration_opens_at"), data.get("goes_live_at"),
                              data.get("archives_at"))
        except ValidationError as exc:
            self.add_error(None, exc)
        return data

    def save(self, commit=True):
        ev = super().save(commit=False)
        cfg = dict(ev.settings or {})
        cfg["disabled_features"] = list(self.cleaned_data.get("disabled_features") or [])
        ev.settings = cfg
        if commit:
            ev.save()
        return ev


class EventCloneForm(PetForm):
    name = forms.CharField(label=_("Name"), max_length=120)
    slug = forms.SlugField(label=_("Slug"))
    start_date = forms.DateField(label=_("Start date"), widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(label=_("End date"), widget=forms.DateInput(attrs={"type": "date"}))

    def clean_slug(self):
        slug = self.cleaned_data["slug"]
        if Event.objects.filter(slug=slug).exists():
            raise forms.ValidationError(_("An event with this slug already exists."))
        return slug


class NumberPlanForm(PetModelForm):
    emergency_numbers = CommaListField(label=_("Emergency numbers"), digits_only=True,
                                       help_text=_("Comma separated. Always blocked and routed to on-site emergency."))

    class Meta:
        model = NumberPlan
        fields = ["min_length", "max_length", "prefix_free", "default_allowed", "default_requires_approval",
                  "test_ringback_number", "wakeup_service_number", "site_survey_number", "echo_test_number",
                  "voicemail_number", "dect_claim_number", "announcement_record_number", "emergency_numbers",
                  "callback_request_code", "callback_cancel_code", "group_login_code", "group_logout_code",
                  "forward_set_code", "forward_clear_code", "forward_busy_code", "forward_noanswer_code"]

    # Field groups for the template: everything before the first feature code vs. the feature codes
    FEATURE_CODE_FIELDS = ["callback_request_code", "callback_cancel_code", "group_login_code", "group_logout_code",
                           "forward_set_code", "forward_clear_code", "forward_busy_code", "forward_noanswer_code"]

    def plan_fields(self):
        return [self[name] for name in self.fields if name not in self.FEATURE_CODE_FIELDS]

    def feature_code_fields(self):
        return [self[name] for name in self.FEATURE_CODE_FIELDS if name in self.fields]

    def clean(self):
        data = super().clean()
        if data.get("min_length") and data.get("max_length") and data["min_length"] > data["max_length"]:
            self.add_error("max_length", _("Maximum length must not be smaller than minimum length."))
        return data


class NumberRangeForm(PetModelForm):
    allowed_roles = forms.MultipleChoiceField(label=_("Allowed roles"), required=False,
                                              choices=EventMembership.Role.choices,
                                              widget=forms.CheckboxSelectMultiple(attrs={"class": "checkbox-list"}))
    allowed_types = forms.MultipleChoiceField(label=_("Allowed extension types"), required=False,
                                              choices=ExtensionType.choices,
                                              widget=forms.CheckboxSelectMultiple(attrs={"class": "checkbox-list"}),
                                              help_text=_("Empty = all types."))

    class Meta:
        model = NumberRange
        fields = ["name", "description", "priority", "is_active", "prefix", "pattern", "min_length", "max_length",
                  "mode", "allowed_roles", "allowed_groups", "allowed_types", "is_vanity", "quota_per_user"]
        widgets = {"allowed_groups": forms.CheckboxSelectMultiple(attrs={"class": "checkbox-list"})}

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["allowed_groups"].queryset = UserGroup.objects.filter(event=event)
        self.fields["allowed_groups"].required = False

    def clean_pattern(self):
        p = self.cleaned_data.get("pattern") or ""
        if p:
            try:
                re.compile(p)
            except re.error as exc:
                raise forms.ValidationError(_("Invalid regular expression: %(e)s") % {"e": exc})
        return p

    def clean(self):
        data = super().clean()
        if not data.get("prefix") and not data.get("pattern"):
            raise forms.ValidationError(_("A range needs a prefix or a pattern."))
        return data


class NumberTestForm(PetForm):
    number = forms.CharField(label=_("Test a number"), max_length=16,
                             widget=forms.TextInput(attrs={"inputmode": "numeric"}))
    as_user = forms.CharField(label=_("Evaluate as user (nickname/e-mail)"), required=False)


class AddMemberForm(PetForm):
    identifier = forms.CharField(label=_("Nickname or e-mail"), max_length=254)
    role = forms.ChoiceField(label=_("Role"), choices=EventMembership.Role.choices,
                             initial=EventMembership.Role.USER)

    def clean_identifier(self):
        u = find_user(self.cleaned_data["identifier"])
        if u is None:
            raise forms.ValidationError(_("No user with that nickname or e-mail address."))
        return u


class MemberUpdateForm(PetModelForm):
    class Meta:
        model = EventMembership
        fields = ["role", "groups"]
        widgets = {"groups": forms.CheckboxSelectMultiple(attrs={"class": "checkbox-list"})}

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["groups"].queryset = UserGroup.objects.filter(event=event)
        self.fields["groups"].required = False


class UserGroupForm(PetModelForm):
    class Meta:
        model = UserGroup
        fields = ["name", "slug", "description", "join_code"]

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.event = event

    def clean_slug(self):
        slug = self.cleaned_data["slug"]
        qs = UserGroup.objects.filter(event=self.event, slug=slug)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(_("A group with this slug already exists in this event."))
        return slug


class GuestCreateForm(PetForm):
    start = forms.CharField(label=_("First number"), required=False, max_length=16,
                            widget=forms.TextInput(attrs={"inputmode": "numeric"}))
    count = forms.IntegerField(label=_("How many"), required=False, min_value=1, max_value=200, initial=10)
    numbers = CommaListField(label=_("…or an explicit list of numbers"), digits_only=True,
                             widget=forms.Textarea(attrs={"rows": 2}),
                             help_text=_("Comma or newline separated. Overrides first number / count."))

    def clean(self):
        data = super().clean()
        if data.get("numbers"):
            return data
        start = (data.get("start") or "").strip()
        if not start or not start.isdigit():
            raise forms.ValidationError(_("Give a numeric first number and a count, or an explicit list."))
        count = data.get("count") or 1
        width = len(start)
        data["numbers"] = [str(int(start) + i).zfill(width) for i in range(count)]
        return data


class WebhookForm(PetModelForm):
    event_types = forms.MultipleChoiceField(label=_("Event types"), required=False, choices=Webhook.EVENT_TYPES,
                                            widget=forms.CheckboxSelectMultiple(attrs={"class": "checkbox-list"}),
                                            help_text=_("Empty = all events."))

    class Meta:
        model = Webhook
        fields = ["name", "url", "secret", "event_types", "is_active"]


class ImportForm(PetForm):
    file = forms.FileField(label=_("Export file (JSON)"))
    slug_override = forms.SlugField(label=_("New slug"), required=False,
                                    help_text=_("Leave empty to keep the slug stored in the file."))


class CSVImportForm(PetForm):
    """Step 1 of the orga CSV import: upload a file or paste the text."""

    MAX_SIZE = 2 * 1024 * 1024

    file = forms.FileField(label=_("CSV file"), required=False,
                           help_text=_("Comma or semicolon separated, UTF-8. First row: column names."))
    text = forms.CharField(label=_("…or paste CSV text"), required=False,
                           widget=forms.Textarea(attrs={"rows": 8, "spellcheck": "false"}))
    create_users = forms.BooleanField(
        label=_("Create missing users"), required=False,
        help_text=_("Unknown e-mail addresses get a new account (no password) and an invitation mail."))

    def clean(self):
        data = super().clean()
        upload = data.get("file")
        text = (data.get("text") or "").strip()
        if upload is not None:
            if upload.size > self.MAX_SIZE:
                raise ValidationError(_("The file is too large (max. 2 MB)."))
            from apps.extensions.csv_import import decode_upload

            text = decode_upload(upload.read())
        if not text.strip():
            raise ValidationError(_("Upload a CSV file or paste CSV text."))
        data["csv_text"] = text
        return data


class ServiceAccountForm(PetForm):
    name = forms.CharField(label=_("Name"), max_length=120)
    description = forms.CharField(label=_("Description"), required=False, max_length=500)
    scopes = CommaListField(label=_("Scopes"), help_text=_(
        "Comma separated, e.g. extensions:read, phonebook:read. Empty = all rights of the owner."))
    expires_at = forms.DateTimeField(label=_("Expires at"), required=False,
                                     widget=forms.DateTimeInput(attrs={"type": "datetime-local"}))


class HelpdeskSearchForm(PetForm):
    q = forms.CharField(label=_("Search"), max_length=120, required=False, widget=forms.TextInput(attrs={
        "placeholder": _("number, nickname, e-mail, IPEI or SIP user"), "autofocus": True}))
