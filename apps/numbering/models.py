"""Number plans: the per-event policy that the orga/mod team controls.

A ``NumberPlan`` holds global length constraints plus an ordered list of
``NumberRange`` rules. Ranges are matched by prefix (or regex) and decide
whether a number is blocked, reserved for a role/group, requires approval,
or is free for instant self-service.

GURU3-style extras live here too: ``ExtensionPool`` (blocks of numbers that
"give me a random number" draws from) and ``ExtensionClaim`` (orga
pre-reservations of a number for a specific person, redeemed via invite link).
"""
import re
import secrets

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel
from apps.extensions.models import ExtensionType

digits = RegexValidator(r"^\d*$", "Digits only")

# Policy denial codes that an orga-driven registration (claims, ``force_active``) may override.
# Emergency/service numbers, blocked ranges and malformed input are never overridable.
SOFT_DENIAL_CODES = frozenset({"length", "closed", "restricted", "type"})


def _new_token():
    return secrets.token_urlsafe(24)


def _default_claim_validity():
    return timezone.now() + timezone.timedelta(days=14)


class NumberPlan(TimeStampedModel):
    event = models.OneToOneField("events.Event", on_delete=models.CASCADE, related_name="number_plan")
    min_length = models.PositiveSmallIntegerField(default=3)
    max_length = models.PositiveSmallIntegerField(default=5)
    default_requires_approval = models.BooleanField(
        default=False, help_text=_("If no range matches: require approval instead of instant activation."),
    )
    default_allowed = models.BooleanField(
        default=True, help_text=_("If no range matches: allow registration at all."),
    )
    prefix_free = models.BooleanField(
        default=True, verbose_name=_("Prefix-free numbering"),
        help_text=_("Variable-length numbers: no extension may be a prefix of another one "
                    "(23 blocks 2323 and vice versa), so every number is dialable without a timeout."),
    )
    # Service extensions that DIAL itself provides (dialable by everyone)
    test_ringback_number = models.CharField(max_length=16, blank=True, default="", validators=[digits])
    wakeup_service_number = models.CharField(max_length=16, blank=True, default="", validators=[digits])
    site_survey_number = models.CharField(max_length=16, blank=True, default="", validators=[digits])
    echo_test_number = models.CharField(max_length=16, blank=True, default="", validators=[digits])
    voicemail_number = models.CharField(max_length=16, blank=True, default="", validators=[digits])
    dect_claim_number = models.CharField(
        max_length=16, blank=True, default="", validators=[digits], verbose_name=_("DECT claim number"),
        help_text=_("Users dial this number followed by the claim code shown on their extension page to bind "
                    "the handset they are calling from to that extension (GURU3-style). Empty = feature off."),
    )
    announcement_record_number = models.CharField(
        max_length=16, blank=True, default="", validators=[digits], verbose_name=_("Announcement recording number"),
        help_text=_("Owners dial this number followed by the recording code shown on their announcement page "
                    "(or enter it after the prompt) to record the announcement with their phone. Empty = feature off."),
    )
    emergency_numbers = models.JSONField(
        default=list, blank=True,
        help_text=_("Numbers that are always blocked from registration and routed to on-site emergency."),
    )
    # Feature codes users dial from handsets
    callback_request_code = models.CharField(max_length=8, default="*66")
    callback_cancel_code = models.CharField(max_length=8, default="*86")
    group_login_code = models.CharField(max_length=8, default="*71")
    group_logout_code = models.CharField(max_length=8, default="*72")
    forward_set_code = models.CharField(
        max_length=8, blank=True, default="*21", verbose_name=_("Forward always (set)"),
        help_text=_("Dial <code><number> to forward all calls to <number>, e.g. *214242. Empty = off."),
    )
    forward_clear_code = models.CharField(
        max_length=8, blank=True, default="*20", verbose_name=_("Forward off (clear)"),
        help_text=_("Dial this code on its own to switch every forwarding of your extension off."),
    )
    forward_busy_code = models.CharField(
        max_length=8, blank=True, default="*22", verbose_name=_("Forward when busy"),
        help_text=_("Dial <code><number> to forward calls to <number> only while you are busy."),
    )
    forward_noanswer_code = models.CharField(
        max_length=8, blank=True, default="*23", verbose_name=_("Forward on no answer"),
        help_text=_("Dial <code><number> to forward calls to <number> when you do not pick up."),
    )

    class Meta:
        verbose_name = _("number plan")

    def __str__(self):
        return f"Number plan for {self.event}"

    # ----------------------------------------------------------------- policy
    def evaluate(self, number: str, *, user=None, extension_type: str | None = None) -> "PolicyResult":
        """Evaluate whether ``number`` may be registered by ``user``.

        Returns a :class:`PolicyResult` describing the outcome. This never
        checks whether the number is *taken* - see ``extensions.services``.
        """
        number = (number or "").strip()
        if not number.isdigit():
            return PolicyResult.deny(_("Extension must consist of digits only."), code="format")
        if number in (self.emergency_numbers or []):
            return PolicyResult.deny(_("This number is reserved for emergency services."), code="emergency")
        for svc in self.service_numbers():
            if number == svc:
                return PolicyResult.deny(_("This number is a system service extension."), code="service")

        rng = self.match_range(number)
        if rng is None:
            if not (self.min_length <= len(number) <= self.max_length):
                return PolicyResult.deny(
                    _("Extension must be between %(min)d and %(max)d digits.")
                    % {"min": self.min_length, "max": self.max_length}, code="length",
                )
            if not self.default_allowed:
                return PolicyResult.deny(_("This number is not available for registration."), code="closed")
            return PolicyResult.allow(requires_approval=self.default_requires_approval)

        return rng.evaluate(number, user=user, extension_type=extension_type)

    def match_range(self, number: str):
        for rng in self.active_ranges():
            if rng.matches(number):
                return rng
        return None

    def active_ranges(self):
        """Active ranges in priority order; served from memory after :meth:`cache_ranges`."""
        cached = getattr(self, "_ranges_cache", None)
        if cached is not None:
            return cached
        return self.ranges.filter(is_active=True).order_by("priority", "id")

    def cache_ranges(self):
        """Opt-in: load ranges once for bulk evaluation (pool scans, random numbers)."""
        self._ranges_cache = list(self.ranges.filter(is_active=True).order_by("priority", "id"))
        return self

    def service_numbers(self) -> list[str]:
        """DIAL-provided service extensions that are configured (dialable by everyone)."""
        return [svc for svc in (self.test_ringback_number, self.wakeup_service_number, self.site_survey_number,
                                self.echo_test_number, self.voicemail_number, self.dect_claim_number,
                                self.announcement_record_number) if svc]

    def feature_codes(self) -> list[str]:
        """All configured feature codes (callback, groups, forwarding)."""
        return [c for c in (self.callback_request_code, self.callback_cancel_code, self.group_login_code,
                            self.group_logout_code, self.forward_set_code, self.forward_clear_code,
                            self.forward_busy_code, self.forward_noanswer_code) if c]

    def reserved_numbers(self) -> list[str]:
        """All digit-only numbers the plan itself occupies: service numbers, emergency numbers and
        feature codes. Codes containing ``*``/``#`` are not dialable as extensions and are ignored."""
        out = list(self.service_numbers()) + [str(n) for n in (self.emergency_numbers or [])]
        out += self.feature_codes()
        return [n for n in out if n and n.isdigit()]

    def prefix_conflicts(self, number: str) -> list[str]:
        """Reserved plan numbers that are a strict prefix of ``number`` or start with it.

        Only meaningful when :attr:`prefix_free` is on; exact matches are handled by :meth:`evaluate`.
        """
        if not self.prefix_free or not number:
            return []
        return sorted({r for r in self.reserved_numbers()
                       if r != number and (number.startswith(r) or r.startswith(number))})

    def clone_to(self, event):
        new = NumberPlan.objects.create(
            event=event,
            **{f.name: getattr(self, f.name) for f in self._meta.fields
               if f.name not in ("id", "event", "created_at", "updated_at")},
        )
        for rng in self.ranges.all():
            groups = list(rng.allowed_groups.values_list("slug", flat=True))
            r = NumberRange.objects.create(
                plan=new,
                **{f.name: getattr(rng, f.name) for f in rng._meta.fields
                   if f.name not in ("id", "plan", "created_at", "updated_at")},
            )
            if groups:
                r.allowed_groups.set(event.groups.filter(slug__in=groups))
        return new


class NumberRange(TimeStampedModel):
    class Mode(models.TextChoices):
        OPEN = "open", _("Open - instant self-service")
        APPROVAL = "approval", _("Requires approval")
        RESTRICTED = "restricted", _("Restricted to roles/groups")
        BLOCKED = "blocked", _("Blocked")

    plan = models.ForeignKey(NumberPlan, on_delete=models.CASCADE, related_name="ranges")
    name = models.CharField(max_length=80)
    description = models.CharField(max_length=200, blank=True)
    priority = models.IntegerField(default=100, help_text=_("Lower matches first."))
    is_active = models.BooleanField(default=True)
    prefix = models.CharField(max_length=16, blank=True, validators=[digits],
                              help_text=_("Match numbers starting with this prefix."))
    pattern = models.CharField(max_length=120, blank=True,
                               help_text=_("Optional regex (anchored) instead of/in addition to prefix."))
    min_length = models.PositiveSmallIntegerField(null=True, blank=True)
    max_length = models.PositiveSmallIntegerField(null=True, blank=True)
    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.OPEN)
    allowed_roles = models.JSONField(
        default=list, blank=True,
        help_text=_("Roles allowed in RESTRICTED mode, e.g. ['orga','helpdesk']."),
    )
    allowed_groups = models.ManyToManyField("events.UserGroup", blank=True,
                                            related_name="allowed_ranges")
    allowed_types = models.JSONField(default=list, blank=True,
                                     help_text=_("Extension types allowed here; empty = all."))
    is_vanity = models.BooleanField(default=False, help_text=_("Premium/vanity - always needs approval."))
    quota_per_user = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["priority", "id"]

    def __str__(self):
        return f"{self.name} ({self.prefix or self.pattern})"

    def matches(self, number: str) -> bool:
        if self.prefix and not number.startswith(self.prefix):
            return False
        if self.pattern:
            try:
                if not re.fullmatch(self.pattern, number):
                    return False
            except re.error:
                return False
        if self.min_length and len(number) < self.min_length:
            return False
        if self.max_length and len(number) > self.max_length:
            return False
        return bool(self.prefix or self.pattern)

    def evaluate(self, number: str, *, user=None, extension_type=None) -> "PolicyResult":
        lo = self.min_length or self.plan.min_length
        hi = self.max_length or self.plan.max_length
        if not (lo <= len(number) <= hi):
            return PolicyResult.deny(
                _("Numbers in range '%(name)s' must be %(lo)d-%(hi)d digits.")
                % {"name": self.name, "lo": lo, "hi": hi}, range=self, code="length",
            )
        if self.mode == self.Mode.BLOCKED:
            return PolicyResult.deny(_("This range is blocked: %(name)s.") % {"name": self.name}, range=self,
                                     code="blocked")
        if self.allowed_types and extension_type and extension_type not in self.allowed_types:
            return PolicyResult.deny(_("This range does not allow this extension type."), range=self, code="type")
        if self.mode == self.Mode.RESTRICTED:
            if not self._user_permitted(user):
                return PolicyResult.deny(
                    _("Range '%(name)s' is restricted to specific roles or groups.") % {"name": self.name},
                    range=self, code="restricted",
                )
        requires_approval = self.is_vanity or self.mode == self.Mode.APPROVAL
        return PolicyResult.allow(requires_approval=requires_approval, range=self)

    def _user_permitted(self, user) -> bool:
        if user is None:
            return False
        if user.is_superuser:
            return True
        event = self.plan.event
        role = user.role_for(event)
        if role and role in (self.allowed_roles or []):
            return True
        if role in ("orga", "admin") and not self.allowed_roles and not self.allowed_groups.exists():
            return True
        user_groups = set(user.groups_for(event))
        return bool(user_groups & set(self.allowed_groups.values_list("slug", flat=True)))


class PolicyResult:
    """Outcome of a number plan evaluation.

    ``code`` names the denial reason (``format``, ``emergency``, ``service``, ``blocked``, ``length``,
    ``closed``, ``restricted``, ``type``) so callers can tell hard blocks from soft ones.
    """

    def __init__(self, allowed: bool, *, reason="", requires_approval=False, range=None, code=""):
        self.allowed = allowed
        self.reason = str(reason) if reason else ""
        self.requires_approval = requires_approval
        self.range = range
        self.code = code

    @classmethod
    def allow(cls, *, requires_approval=False, range=None):
        return cls(True, requires_approval=requires_approval, range=range)

    @classmethod
    def deny(cls, reason, *, range=None, code=""):
        return cls(False, reason=reason, range=range, code=code)

    def __bool__(self):
        return self.allowed

    @property
    def overridable(self) -> bool:
        """True if the denial may be lifted by an orga-driven registration (claim redemption, force_active)."""
        return not self.allowed and self.code in SOFT_DENIAL_CODES

    def as_dict(self):
        return {
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "reason": self.reason,
            "code": self.code,
            "range": self.range.name if self.range else None,
        }


class ExtensionPool(TimeStampedModel):
    """A block of numbers (``prefix`` + digits up to ``length``) that random-number requests draw from."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="extension_pools")
    name = models.CharField(max_length=80)
    prefix = models.CharField(max_length=16, blank=True, validators=[digits],
                              help_text=_("All numbers in the pool start with these digits."))
    length = models.PositiveSmallIntegerField(default=4, help_text=_("Total number of digits."))
    is_active = models.BooleanField(default=True)
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["prefix", "length", "id"]
        verbose_name = _("extension pool")

    def __str__(self):
        return f"{self.name} ({self.mask})"

    def clean(self):
        super().clean()
        if self.length > 16:
            raise ValidationError({"length": _("Numbers may have at most 16 digits.")})
        if len(self.prefix or "") >= self.length:
            raise ValidationError({"prefix": _("The prefix must be shorter than the total length.")})

    @property
    def free_digits(self) -> int:
        return max(self.length - len(self.prefix or ""), 0)

    @property
    def mask(self) -> str:
        """``23xx`` - the pool's shape for display."""
        return f"{self.prefix}{'x' * self.free_digits}"

    @property
    def size(self) -> int:
        return 10 ** self.free_digits

    def candidates(self):
        """Yield every number of this pool in ascending order."""
        width = self.free_digits
        for i in range(self.size):
            yield f"{self.prefix}{i:0{width}d}"

    def random_candidate(self) -> str:
        return f"{self.prefix}{secrets.randbelow(self.size):0{self.free_digits}d}"

    def free_numbers(self, limit: int | None = None, *, user=None, extension_type=None) -> list[str]:
        """Numbers of this pool that are neither taken/conflicting nor denied or approval-gated by the plan."""
        from .services import free_numbers_in

        return free_numbers_in(self.event, self.candidates(), limit=limit, user=user, extension_type=extension_type)


class ExtensionClaimQuerySet(models.QuerySet):
    def open(self):
        return self.filter(redeemed_at__isnull=True, valid_until__gt=timezone.now())

    def for_event(self, event):
        return self.filter(event=event)


class ExtensionClaim(TimeStampedModel):
    """An orga pre-reservation of ``number`` for one person, redeemed through an invite link.

    While open, the number counts as taken for everyone except the claimant (matched by ``user`` or,
    for people without an account yet, by ``email``).
    """

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="extension_claims")
    number = models.CharField(max_length=16, validators=[digits], db_index=True)
    type = models.CharField(max_length=20, choices=ExtensionType.choices, default=ExtensionType.DECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE,
                             related_name="extension_claims")
    email = models.EmailField(blank=True, help_text=_("Invite address if the person has no account yet."))
    token = models.CharField(max_length=64, unique=True, default=_new_token)
    valid_until = models.DateTimeField(default=_default_claim_validity)
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    redeemed_at = models.DateTimeField(null=True, blank=True)
    redeemed_extension = models.ForeignKey("extensions.Extension", null=True, blank=True,
                                           on_delete=models.SET_NULL, related_name="claims")

    objects = ExtensionClaimQuerySet.as_manager()

    class Meta:
        ordering = ["number", "id"]
        verbose_name = _("extension claim")

    def __str__(self):
        return f"claim {self.number} for {self.claimant_label} ({self.event.slug})"

    @property
    def is_redeemed(self) -> bool:
        return self.redeemed_at is not None

    @property
    def is_expired(self) -> bool:
        return not self.is_redeemed and self.valid_until <= timezone.now()

    @property
    def is_open(self) -> bool:
        return not self.is_redeemed and self.valid_until > timezone.now()

    @property
    def status(self) -> str:
        if self.is_redeemed:
            return "redeemed"
        return "open" if self.is_open else "expired"

    @property
    def claimant_label(self) -> str:
        if self.user_id:
            return self.user.username
        return self.email or "?"

    @property
    def invite_email(self) -> str:
        return self.email or (self.user.email if self.user_id else "")

    def is_for(self, user) -> bool:
        """Is ``user`` the intended claimant?"""
        if user is None or not getattr(user, "is_authenticated", True):
            return False
        if self.user_id:
            return self.user_id == user.pk
        return bool(self.email) and bool(user.email) and self.email.lower() == user.email.lower()

    def redeem_path(self) -> str:
        from django.urls import reverse

        return reverse("numbering:claim_redeem", args=[self.event.slug, self.token])
