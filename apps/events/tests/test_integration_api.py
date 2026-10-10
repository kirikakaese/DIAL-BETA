"""What integrations (e.g. EVAC) rely on: webhook delivery ids, members with e-mail, the token in ``me/``."""
import contextlib
from unittest import mock

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import ServiceAccount
from apps.events import webhooks
from apps.events.models import Webhook

pytestmark = pytest.mark.django_db


def test_webhook_delivery_id_is_stable_across_retries(event, settings):
    Webhook.objects.create(event=event, name="evac", url="https://evac.example.org/hook", secret="s3cret",
                           event_types=[], is_active=True)
    queued = []
    with mock.patch.object(webhooks.deliver, "delay", side_effect=lambda *a: queued.append(a)):
        webhooks.emit("page.updated", {"id": 1}, event=event)
    assert len(queued) == 1 and len(queued[0][3]) == 36
    hook_id, kind, payload, delivery = queued[0]
    sent = []

    class Resp:
        status_code = 500

        def raise_for_status(self):
            raise RuntimeError("down")

    def post(url, data, headers, timeout):
        sent.append(headers)
        return Resp()

    # a retry runs the task again with the same arguments: the delivery id is part of them
    with mock.patch("requests.post", side_effect=post):
        for _attempt in range(2):
            with contextlib.suppress(Exception):  # the failed attempt asks Celery for a retry
                webhooks.deliver.apply(args=(hook_id, kind, payload, delivery))
    assert len(sent) == 2 and {h["X-DIAL-Delivery"] for h in sent} == {delivery}
    assert all(h["X-DIAL-Signature"].startswith("sha256=") for h in sent)


def test_members_carry_email_for_helpdesk(event, orga, member, user):
    client = APIClient()
    client.force_authenticate(orga)
    r = client.get(f"/api/v1/events/{event.slug}/members/")
    by = {m["user"]: m for m in r.json()}
    assert by["alice"]["email"] == "alice@example.org" and by["orga"]["role"] == "orga"
    client.force_authenticate(user)
    assert client.get(f"/api/v1/events/{event.slug}/members/").status_code == 403


def test_me_shows_the_calling_token(event, orga):
    acct, raw = ServiceAccount.issue(name="evac", owner=orga, event=event, scopes=["pages:read", "emergency:*"])
    r = APIClient(HTTP_AUTHORIZATION=f"Bearer {raw}").get("/api/v1/me/")
    tok = r.json()["token"]
    assert tok == {"name": "evac", "prefix": acct.token_prefix, "scopes": ["pages:read", "emergency:*"],
                   "event": "demo", "expires_at": None}
    client = APIClient()
    client.force_authenticate(orga)
    assert client.get("/api/v1/me/").json()["token"] is None
