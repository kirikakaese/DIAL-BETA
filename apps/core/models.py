"""Core models shared by every PET app."""
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.translation import gettext_lazy as _


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditLog(models.Model):
    """Immutable record of who did what, to which object, in which event."""

    class Action(models.TextChoices):
        CREATE = "create", _("Create")
        UPDATE = "update", _("Update")
        DELETE = "delete", _("Delete")
        APPROVE = "approve", _("Approve")
        REJECT = "reject", _("Reject")
        LOGIN = "login", _("Login")
        PROVISION = "provision", _("Provision")
        TRANSFER = "transfer", _("Transfer")
        IMPERSONATE = "impersonate", _("Impersonate")
        OTHER = "other", _("Other")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="audit_entries",
    )
    actor_repr = models.CharField(max_length=200, blank=True)
    event = models.ForeignKey(
        "events.Event", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="audit_entries",
    )
    action = models.CharField(max_length=20, choices=Action.choices, db_index=True)
    target_type = models.ForeignKey(ContentType, null=True, blank=True, on_delete=models.SET_NULL)
    target_id = models.CharField(max_length=64, blank=True, db_index=True)
    target = GenericForeignKey("target_type", "target_id")
    target_repr = models.CharField(max_length=300, blank=True)
    message = models.CharField(max_length=500, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["target_type", "target_id"])]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.actor_repr} {self.action} {self.target_repr}"
