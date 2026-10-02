"""Global user accounts.

Users have exactly one account across all events. Per-event roles live in
``events.EventMembership``; global admin is ``is_staff``/``is_superuser``.
"""
import datetime as dt
import hashlib
import secrets
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_active", True)
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(_("email address"), unique=True)
    username = models.CharField(
        _("nickname"), max_length=64, unique=True,
        help_text=_("Public handle shown in phonebooks."),
    )
    display_name = models.CharField(max_length=120, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    email_verified = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    # External identity (OIDC / LDAP). Optional.
    oidc_subject = models.CharField(max_length=255, blank=True, db_index=True)
    ldap_dn = models.CharField(max_length=255, blank=True)
    # Privacy: GDPR data-export/delete flags
    gdpr_erasure_requested_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    objects = UserManager()

    class Meta:
        ordering = ["username"]

    def __str__(self):
        return self.display_name or self.username

    def get_short_name(self):
        return self.username

    def get_full_name(self):
        return self.display_name or self.username

    # --- role helpers ------------------------------------------------------
    def role_for(self, event):
        """Return the EventMembership role for ``event`` or ``None``."""
        if self.is_superuser:
            return "admin"
        m = event.memberships.filter(user=self).first()
        return m.role if m else None

    def is_orga(self, event) -> bool:
        return self.is_superuser or event.memberships.filter(
            user=self, role__in=("orga", "admin")
        ).exists()

    def is_helpdesk(self, event) -> bool:
        return self.is_superuser or event.memberships.filter(
            user=self, role__in=("orga", "admin", "helpdesk")
        ).exists()

    def groups_for(self, event):
        """Slugs of user groups the user belongs to within ``event``."""
        from apps.events.models import EventMembership

        m = EventMembership.objects.filter(user=self, event=event).first()
        return list(m.groups.values_list("slug", flat=True)) if m else []


class ServiceAccount(models.Model):
    """Token-based service accounts for integrations (badge systems, info-beamer...)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="service_accounts")
    event = models.ForeignKey(
        "events.Event", null=True, blank=True, on_delete=models.CASCADE,
        related_name="service_accounts",
        help_text=_("Restrict the account to one event; empty = global."),
    )
    scopes = models.JSONField(
        default=list, blank=True,
        help_text=_("List of scopes, e.g. ['extensions:read', 'phonebook:read']. Empty = all of owner's rights."),
    )
    token_hash = models.CharField(max_length=128, unique=True, editable=False)
    token_prefix = models.CharField(max_length=12, editable=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.token_prefix}…)"

    @staticmethod
    def hash_token(raw: str) -> str:
        import hashlib

        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def issue(cls, *, name, owner, event=None, scopes=None, expires_at=None, description=""):
        """Create a service account and return ``(account, raw_token)``. The raw
        token is only available at creation time."""
        import secrets

        raw = "pet_" + secrets.token_urlsafe(32)
        acct = cls.objects.create(
            name=name, owner=owner, event=event, scopes=scopes or [], expires_at=expires_at,
            description=description, token_hash=cls.hash_token(raw), token_prefix=raw[:12],
        )
        return acct, raw

    def has_scope(self, scope: str) -> bool:
        if not self.scopes:
            return True
        if scope in self.scopes:
            return True
        # 'extensions:*' style wildcards
        ns = scope.split(":")[0]
        return f"{ns}:*" in self.scopes or "*" in self.scopes


class TokenError(Exception):
    """Raised by :meth:`RegistrationEmailToken.redeem` for unknown, expired or already used tokens."""


class RegistrationEmailTokenManager(models.Manager):
    def purge_expired(self) -> int:
        """Delete tokens whose lifetime has passed (used or not). Returns the number deleted."""
        deleted, _ = self.filter(expires_at__lt=timezone.now()).delete()
        return deleted


class RegistrationEmailToken(models.Model):
    """Single-use e-mail confirmation link for signup, address verification and address change.

    Only the SHA-256 of the raw token is stored; the raw token lives in the e-mail link only.
    """

    class Purpose(models.TextChoices):
        REGISTER = "register", _("Registration")
        VERIFY = "verify", _("Verify e-mail")
        CHANGE_EMAIL = "change_email", _("Change e-mail")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(db_index=True, help_text=_("Address the link was sent to (lowercase)."))
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    purpose = models.CharField(max_length=16, choices=Purpose.choices)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.CASCADE, related_name="email_tokens")
    new_email = models.EmailField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)

    objects = RegistrationEmailTokenManager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_purpose_display()} <{self.email}>"

    @staticmethod
    def normalize(email: str) -> str:
        return User.objects.normalize_email(email or "").strip().lower()

    @staticmethod
    def hash_token(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def issue(cls, email, purpose, user=None, new_email="", ip=None):
        """Create a token and return ``(token, raw_token)``. The raw token is never stored."""
        raw = secrets.token_urlsafe(32)
        ttl = getattr(settings, "PET_EMAIL_TOKEN_TTL_HOURS", 48)
        obj = cls.objects.create(
            email=cls.normalize(email), purpose=purpose, user=user, new_email=cls.normalize(new_email),
            token_hash=cls.hash_token(raw), expires_at=timezone.now() + dt.timedelta(hours=ttl), ip=ip or None,
        )
        return obj, raw

    @property
    def is_valid(self) -> bool:
        return self.used_at is None and self.expires_at > timezone.now()

    @classmethod
    def lookup(cls, raw, purpose=None):
        """Return the valid token for ``raw`` without consuming it. Raises :class:`TokenError`."""
        if not raw or len(raw) > 128:
            raise TokenError("invalid")
        tok = cls.objects.select_related("user").filter(token_hash=cls.hash_token(raw)).first()
        if tok is None or (purpose and tok.purpose != purpose):
            raise TokenError("invalid")
        if tok.used_at is not None:
            raise TokenError("used")
        if tok.expires_at <= timezone.now():
            raise TokenError("expired")
        return tok

    @classmethod
    def redeem(cls, raw, purpose=None):
        """Validate ``raw`` and mark the token as used. Raises :class:`TokenError`."""
        tok = cls.lookup(raw, purpose)
        tok.mark_used()
        return tok

    def mark_used(self):
        self.used_at = timezone.now()
        self.save(update_fields=["used_at"])
