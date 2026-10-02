"""Webhook dispatch (Celery task + helper)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from celery import shared_task
from django.utils import timezone

from .models import Webhook

log = logging.getLogger("dial.webhooks")


def emit(event_type: str, payload: dict, event=None) -> None:
    """Queue delivery of ``payload`` to every matching webhook subscription."""
    from apps.core.features import enabled

    if not enabled("webhooks"):
        return
    qs = Webhook.objects.filter(is_active=True)
    if event is not None:
        from django.db.models import Q

        qs = qs.filter(Q(event=event) | Q(event__isnull=True))
    for hook in qs:
        if hook.matches(event_type):
            deliver.delay(hook.pk, event_type, payload)


@shared_task(bind=True, max_retries=5, default_retry_delay=30)
def deliver(self, hook_id: int, event_type: str, payload: dict):
    import requests

    try:
        hook = Webhook.objects.get(pk=hook_id)
    except Webhook.DoesNotExist:
        return
    body = json.dumps(
        {"type": event_type, "sent_at": timezone.now().isoformat(), "data": payload},
        default=str,
    ).encode()
    headers = {"Content-Type": "application/json", "X-DIAL-Event": event_type}
    if hook.secret:
        sig = hmac.new(hook.secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-DIAL-Signature"] = f"sha256={sig}"
    try:
        resp = requests.post(hook.url, data=body, headers=headers, timeout=10)
        hook.last_status = str(resp.status_code)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        hook.last_status = f"error: {exc}"[:40]
        hook.last_delivery_at = timezone.now()
        hook.save(update_fields=["last_status", "last_delivery_at"])
        log.warning("webhook %s delivery failed: %s", hook, exc)
        raise self.retry(exc=exc)
    hook.last_delivery_at = timezone.now()
    hook.save(update_fields=["last_status", "last_delivery_at"])
