"""Events: the multi-tenant root of everything in DIAL."""
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel

# Lifecycle order used by scheduled transitions: a schedule only fires for a state *ahead* of the current one.
LIFECYCLE_ORDER = ["draft", "registration", "live", "archived"]
# target state -> Event field holding the scheduled timestamp (in lifecycle order)
SCHEDULE_FIELDS = {
    "registration": "registration_opens_at",
    "live": "goes_live_at",
    "archived": "archives_at",
}


def state_index(state: str) -> int:
    return LIFECYCLE_ORDER.index(state)


def validate_schedule(state: str, registration_opens_at=None, goes_live_at=None, archives_at=None) -> None:
    """Shared by the portal form and the API serializer.

    Raises a field-keyed :class:`~django.core.exceptions.ValidationError` if the set timestamps are not in
    increasing lifecycle order or if a schedule targets a state the event (in ``state``) has already reached.
    """
    values = {"registration_opens_at": registration_opens_at, "goes_live_at": goes_live_at,
              "archives_at": archives_at}
    errors: dict[str, str] = {}
    current = state_index(state)
    for target, field in SCHEDULE_FIELDS.items():
        if values[field] is not None and state_index(target) <= current:
            errors[field] = _("The event has already reached this state.")
    previous = None
    for field in SCHEDULE_FIELDS.values():
        value = values[field]
        if value is None:
            continue
        if previous is not None and value <= previous[1]:
            errors.setdefault(field, _("Must be later than %(field)s.") % {
                "field": Event._meta.get_field(previous[0]).verbose_name})
        previous = (field, value)
    if errors:
        raise ValidationError(errors)


class EventQuerySet(models.QuerySet):
    def visible_to(self, user):
        """Events a user may see: public non-draft events plus events they are a member of."""
        if user.is_superuser:
            return self
        q = Q(is_public=True) & ~Q(state=Event.State.DRAFT)
        if user.is_authenticated:
            q |= Q(memberships__user=user)
        return self.filter(q).distinct()

    def live(self):
        return self.filter(state__in=[Event.State.REGISTRATION, Event.State.LIVE])


class Event(TimeStampedModel):
    class State(models.TextChoices):
        DRAFT = "draft", _("Draft")
        REGISTRATION = "registration", _("Registration open")
        LIVE = "live", _("Live")
        ARCHIVED = "archived", _("Archived")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    state = models.CharField(max_length=20, choices=State.choices, default=State.DRAFT, db_index=True)
    is_public = models.BooleanField(default=True, help_text=_("Listed publicly and joinable by any user."))
    start_date = models.DateField()
    end_date = models.DateField()
    location = models.CharField(max_length=200, blank=True)
    timezone = models.CharField(max_length=64, default="Europe/Berlin")
    # Branding
    logo = models.ImageField(upload_to="events/logos/", blank=True, null=True)
    primary_color = models.CharField(max_length=7, default="#3b82f6")
    accent_color = models.CharField(max_length=7, default="#22d3ee")
    announcement = models.TextField(blank=True, help_text=_("Shown on the event dashboard."))
    # Dial plan
    sip_domain = models.CharField(max_length=120, blank=True, help_text=_("SIP realm, e.g. event.dial.local"))
    dial_prefix = models.CharField(
        max_length=8, blank=True,
        help_text=_("Prefix used by federated peers to reach this event (DIAL-VPN)."),
    )
    default_language = models.CharField(
        max_length=8, default="en", choices=settings.DIAL_PBX_LANGUAGES, verbose_name=_("Announcement language"),
        help_text=_("Asterisk sound pack for voicemail prompts and system announcements; extensions may override it. "
                    "The web interface itself is English only."),
    )
    # GSM (on-site cell network, e.g. Osmocom)
    has_gsm = models.BooleanField(
        default=False,
        help_text=_("An on-site GSM network (e.g. Osmocom/OpenBSC) is connected to this event's PBX."),
    )
    gsm_trunk = models.CharField(
        max_length=60, blank=True, default="gsm-gateway",
        help_text=_("PJSIP endpoint name of the GSM gateway trunk."),
    )
    # Quotas / policies (numbering policy proper lives in numbering.NumberPlan)
    max_extensions_per_user = models.PositiveIntegerField(default=3)
    allow_guest_extensions = models.BooleanField(default=False)
    allow_breakout = models.BooleanField(default=False)
    cdr_aggregate_only = models.BooleanField(
        default=False, help_text=_("Privacy: only keep aggregate statistics, no per-call records."),
    )
    cdr_retention_days = models.PositiveIntegerField(null=True, blank=True)
    # Scheduled lifecycle transitions (applied by apps.events.tasks.apply_scheduled_transitions)
    registration_opens_at = models.DateTimeField(
        _("registration opens at"), null=True, blank=True,
        help_text=_("Automatically open registration at this time."),
    )
    goes_live_at = models.DateTimeField(
        _("goes live at"), null=True, blank=True,
        help_text=_("Automatically switch the event to live at this time."),
    )
    archives_at = models.DateTimeField(
        _("archives at"), null=True, blank=True,
        help_text=_("Automatically archive the event at this time."),
    )
    # Cloning
    cloned_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="clones",
    )
    settings = models.JSONField(default=dict, blank=True)

    objects = EventQuerySet.as_manager()

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return self.name

    @property
    def is_live(self):
        return self.state == self.State.LIVE

    @property
    def is_archived(self):
        return self.state == self.State.ARCHIVED

    @property
    def registration_open(self):
        return self.state in (self.State.REGISTRATION, self.State.LIVE)

    @property
    def days_remaining(self):
        return (self.end_date - timezone.localdate()).days

    @property
    def tzinfo(self):
        """The event's timezone as ``ZoneInfo`` (falls back to the server timezone if the name is invalid)."""
        try:
            return ZoneInfo(self.timezone or settings.TIME_ZONE)
        except (ZoneInfoNotFoundError, ValueError):
            return timezone.get_default_timezone()

    def is_ahead(self, state: str) -> bool:
        """True if ``state`` comes after the current state in the lifecycle order."""
        return state_index(state) > state_index(self.state)

    def next_scheduled_transition(self):
        """``(state, when)`` of the earliest schedule targeting a state still ahead of the current one, or None."""
        pending = [(target, getattr(self, field)) for target, field in SCHEDULE_FIELDS.items()
                   if getattr(self, field) is not None and self.is_ahead(target)]
        if not pending:
            return None
        return min(pending, key=lambda item: item[1])

    def validate_schedule(self):
        validate_schedule(self.state, self.registration_opens_at, self.goes_live_at, self.archives_at)

    def transition(self, new_state: str, actor=None, *, message: str = "State change"):
        allowed = {
            self.State.DRAFT: {self.State.REGISTRATION},
            self.State.REGISTRATION: {self.State.LIVE, self.State.DRAFT},
            self.State.LIVE: {self.State.ARCHIVED, self.State.REGISTRATION},
            self.State.ARCHIVED: {self.State.LIVE},
        }
        if new_state not in allowed[self.State(self.state)]:
            raise ValueError(f"Cannot move event from {self.state} to {new_state}")
        old = self.state
        self.state = new_state
        fields = ["state", "updated_at"]
        # A schedule for a state we have now reached (or passed) is moot - drop it so it cannot fire later.
        for target, field in SCHEDULE_FIELDS.items():
            if getattr(self, field) is not None and not self.is_ahead(target):
                setattr(self, field, None)
                fields.append(field)
        self.save(update_fields=fields)
        from apps.core.audit import log

        log(action="update", actor=actor, target=self, event=self,
            message=message, changes={"state": [old, new_state]})

    def clone(self, *, name, slug, start_date, end_date, actor=None):
        """Create a new event with the same number plan, groups and branding."""
        from apps.numbering.models import NumberPlan
        from apps.pages.models import InfoPage

        new = Event.objects.create(
            name=name, slug=slug, start_date=start_date, end_date=end_date,
            description=self.description, location=self.location, timezone=self.timezone,
            primary_color=self.primary_color, accent_color=self.accent_color,
            sip_domain=self.sip_domain, dial_prefix=self.dial_prefix,
            default_language=self.default_language,
            max_extensions_per_user=self.max_extensions_per_user,
            allow_guest_extensions=self.allow_guest_extensions,
            allow_breakout=self.allow_breakout, cdr_aggregate_only=self.cdr_aggregate_only,
            cdr_retention_days=self.cdr_retention_days, cloned_from=self, settings=dict(self.settings),
        )
        for g in self.groups.all():
            UserGroup.objects.create(event=new, name=g.name, slug=g.slug, description=g.description)
        for p in self.pages.all():
            InfoPage.objects.create(event=new, slug=p.slug, title=p.title, body=p.body, order=p.order,
                                    published=p.published, show_on_dashboard=p.show_on_dashboard)
        for m in self.memberships.filter(role__in=("orga", "admin")):
            EventMembership.objects.create(event=new, user=m.user, role=m.role)
        plan = NumberPlan.objects.filter(event=self).first()
        if plan:
            plan.clone_to(new)
        from apps.core.audit import log

        log(action="create", actor=actor, target=new, event=new, message=f"Cloned from {self.slug}")
        return new


class UserGroup(TimeStampedModel):
    """Per-event user groups (e.g. 'angels', 'orga', 'medics') used by number
    plan range restrictions and hunt-group membership."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="groups")
    name = models.CharField(max_length=80)
    slug = models.SlugField()
    description = models.CharField(max_length=200, blank=True)
    join_code = models.CharField(
        max_length=32, blank=True,
        help_text=_("Optional code users can enter to self-join this group."),
    )

    class Meta:
        unique_together = [("event", "slug")]
        ordering = ["name"]

    def __str__(self):
        return f"{self.event.slug}:{self.slug}"


class EventMembership(TimeStampedModel):
    class Role(models.TextChoices):
        USER = "user", _("User")
        HELPDESK = "helpdesk", _("Helpdesk")
        ORGA = "orga", _("Orga / Moderator")
        ADMIN = "admin", _("Event admin")

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.USER)
    groups = models.ManyToManyField(UserGroup, blank=True, related_name="members")
    notes = models.TextField(blank=True)

    class Meta:
        unique_together = [("event", "user")]

    def __str__(self):
        return f"{self.user} @ {self.event.slug} ({self.role})"

    @property
    def is_orga(self):
        return self.role in (self.Role.ORGA, self.Role.ADMIN)


class Webhook(TimeStampedModel):
    """Outgoing webhook subscriptions for extension lifecycle and infra alerts."""

    EVENT_TYPES = [
        ("extension.created", "extension.created"),
        ("extension.approved", "extension.approved"),
        ("extension.rejected", "extension.rejected"),
        ("extension.updated", "extension.updated"),
        ("extension.deleted", "extension.deleted"),
        ("extension.transferred", "extension.transferred"),
        ("callgroup.invited", "callgroup.invited"),
        ("callgroup.invite_accepted", "callgroup.invite_accepted"),
        ("callgroup.invite_declined", "callgroup.invite_declined"),
        ("device.provisioned", "device.provisioned"),
        ("device.claimed", "device.claimed"),
        ("device.adopted", "device.adopted"),
        ("dect.rfp.down", "dect.rfp.down"),
        ("dect.rfp.up", "dect.rfp.up"),
        ("dect.sync.degraded", "dect.sync.degraded"),
        ("callback.completed", "callback.completed"),
        ("emergency.triggered", "emergency.triggered"),
        ("page.updated", "page.updated"),
        ("announcement.recorded", "announcement.recorded"),
        ("pbx.snapshot.changed", "pbx.snapshot.changed"),
    ]

    event = models.ForeignKey(Event, null=True, blank=True, on_delete=models.CASCADE, related_name="webhooks")
    name = models.CharField(max_length=120)
    url = models.URLField()
    secret = models.CharField(max_length=128, blank=True, help_text=_("HMAC-SHA256 signing secret."))
    event_types = models.JSONField(default=list, help_text=_("List of event types; empty = all."))
    is_active = models.BooleanField(default=True)
    last_status = models.CharField(max_length=40, blank=True)
    last_delivery_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name

    def matches(self, event_type: str) -> bool:
        return self.is_active and (not self.event_types or event_type in self.event_types)
