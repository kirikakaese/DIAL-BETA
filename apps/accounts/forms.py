"""Account forms: registration, login, profile, service tokens, GDPR."""
from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm, UserCreationForm
from django.utils.translation import gettext_lazy as _

from apps.core.antispam import SpamGuardMixin
from apps.events.models import Event
from apps.portal.forms import AccessibleFormMixin, CommaListField

from .models import User


def validate_signup_email(email: str) -> str:
    """Apply the instance's domain allow/block lists (``PET_SIGNUP_ALLOWED_DOMAINS`` / ``_BLOCKED_DOMAINS``).

    Both compare the full domain and parent domains, so ``example.org`` also covers ``mail.example.org``.
    """
    domain = email.rsplit("@", 1)[-1].lower().strip(".")
    parts = domain.split(".")
    candidates = {".".join(parts[i:]) for i in range(len(parts))}
    blocked = {d.lower().lstrip("@.") for d in getattr(settings, "PET_SIGNUP_BLOCKED_DOMAINS", []) if d}
    allowed = {d.lower().lstrip("@.") for d in getattr(settings, "PET_SIGNUP_ALLOWED_DOMAINS", []) if d}
    if candidates & blocked:
        raise forms.ValidationError(_("Sign-ups from this e-mail provider are not accepted here."), code="blocked")
    if allowed and not (candidates & allowed):
        raise forms.ValidationError(
            _("Sign-ups are limited to these e-mail domains: %(d)s") % {"d": ", ".join(sorted(allowed))},
            code="not_allowed")
    return email


class RegisterForm(SpamGuardMixin, AccessibleFormMixin, UserCreationForm):
    email = forms.EmailField(label=_("E-mail address"), widget=forms.EmailInput(attrs={"autocomplete": "email"}))

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("email", "username")
        labels = {"username": _("Nickname")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.setdefault("autocomplete", "nickname")

    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data["email"])
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(_("An account with this e-mail address already exists."))
        return validate_signup_email(email)


class RegisterEmailForm(SpamGuardMixin, AccessibleFormMixin, forms.Form):
    """Step 1 of e-mail-first signup: only the address. Never reveals existing accounts."""

    email = forms.EmailField(label=_("E-mail address"),
                             widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}))

    def clean_email(self):
        return validate_signup_email(User.objects.normalize_email(self.cleaned_data["email"]).lower())


class RegisterConfirmForm(AccessibleFormMixin, UserCreationForm):
    """Step 2 of e-mail-first signup: the address comes from the confirmed token."""

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username",)
        labels = {"username": _("Nickname")}

    def __init__(self, *args, email, **kwargs):
        super().__init__(*args, **kwargs)
        self.email = email
        self.instance.email = email
        self.instance.email_verified = True
        self.fields["username"].widget.attrs.setdefault("autocomplete", "nickname")


class ChangeEmailForm(AccessibleFormMixin, forms.Form):
    new_email = forms.EmailField(label=_("New e-mail address"),
                                 widget=forms.EmailInput(attrs={"autocomplete": "email"}))

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_new_email(self):
        email = User.objects.normalize_email(self.cleaned_data["new_email"]).lower()
        if email == (self.user.email or "").lower():
            raise forms.ValidationError(_("This is already your e-mail address."))
        if User.objects.filter(email__iexact=email).exclude(pk=self.user.pk).exists():
            raise forms.ValidationError(_("An account with this e-mail address already exists."))
        return validate_signup_email(email)


class LoginForm(SpamGuardMixin, AccessibleFormMixin, AuthenticationForm):
    check_timing = False  # honeypot only: password managers legitimately submit within a second
    remember_me = forms.BooleanField(label=_("Keep me logged in"), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = _("E-mail address")
        self.fields["username"].widget.attrs.update({"autocomplete": "email", "type": "email", "autofocus": True})


class GuardedPasswordResetForm(SpamGuardMixin, AccessibleFormMixin, PasswordResetForm):
    pass


class ProfileForm(AccessibleFormMixin, forms.ModelForm):
    """Profile basics. The e-mail address is changed via :class:`ChangeEmailForm` (confirmation link)."""

    class Meta:
        model = User
        fields = ["display_name"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["display_name"].widget.attrs.setdefault("autocomplete", "name")


class ServiceAccountForm(AccessibleFormMixin, forms.Form):
    name = forms.CharField(label=_("Name"), max_length=120)
    description = forms.CharField(label=_("Description"), required=False, max_length=500)
    event = forms.ModelChoiceField(label=_("Restrict to event"), required=False, queryset=Event.objects.none(),
                                   empty_label=_("— global (all my events) —"))
    scopes = CommaListField(label=_("Scopes"), help_text=_(
        "Comma separated, e.g. extensions:read, phonebook:read. Empty = all of your rights."))
    expires_at = forms.DateTimeField(label=_("Expires at"), required=False,
                                     widget=forms.DateTimeInput(attrs={"type": "datetime-local"}))

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["event"].queryset = Event.objects.visible_to(user).order_by("-start_date")


class GdprDeleteForm(AccessibleFormMixin, forms.Form):
    confirm = forms.BooleanField(label=_("I understand that all my extensions will be deleted and my account "
                                         "will be deactivated permanently."))
