"""Celery housekeeping for accounts."""
from celery import shared_task


@shared_task
def purge_email_tokens() -> int:
    """Drop expired/used registration, verification and e-mail-change tokens."""
    from .models import RegistrationEmailToken

    return RegistrationEmailToken.objects.purge_expired()
