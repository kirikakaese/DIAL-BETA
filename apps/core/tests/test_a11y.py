"""Every server-rendered page from the smoke-test URL list passes ``apps.core.a11y.check_html``.

The demo event is seeded once per module with ``seed_demo`` (committed outside the per-test transaction)
and removed again at teardown so the other test modules keep working on an empty database.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.test import Client

from apps.core.a11y import SKIP_PREFIXES, audit_url, smoke_page_lists

PLACEHOLDERS = dict(S="demo", ext_pk="{ext_pk}", ext_number="{ext_number}", dev_pk="{dev_pk}",
                    pb_token="{pb_token}", vendors=(), prov_token=None)
PAGES = smoke_page_lists(**PLACEHOLDERS)
PERSONAS = [("anon", "anon"), ("user", "user"), ("orga", "orga"), ("admin", "orga")]
CASES = [(who, url) for who, key in PERSONAS for url in PAGES[key]
         if not url.startswith(SKIP_PREFIXES) and not url.startswith("/api/")
         and not url.endswith((".png", ".pdf", ".csv", ".vcf", ".ldif", ".xml"))]


@pytest.fixture(scope="module")
def demo(django_db_setup, django_db_blocker):
    from apps.accounts.models import User
    from apps.devices.models import Device
    from apps.events.models import Event
    from apps.extensions.models import Extension
    from apps.phonebook import services as pb_services

    with django_db_blocker.unblock():
        call_command("seed_demo", "--no-cdr", verbosity=0)
        event = Event.objects.get(slug="demo")
        alice = User.objects.get(email="alice@dial.local")
        ext = Extension.objects.filter(event=event, owner=alice, state="active").first()
        dev = Device.objects.filter(event=event, owner=alice).first()
        ctx = {
            "users": {
                "anon": None,
                "user": alice,
                "orga": User.objects.get(email="orga@dial.local"),
                "admin": User.objects.get(email="admin@dial.local"),
            },
            "fmt": dict(ext_pk=ext.pk, ext_number=ext.number, dev_pk=dev.pk,
                        pb_token=pb_services.get_settings(event).directory_token),
        }
    yield ctx
    with django_db_blocker.unblock():
        from apps.core.models import AuditLog
        from apps.pbx.models import PBXJob, VoicemailUser

        Event.objects.filter(slug="demo").delete()
        User.objects.filter(email__endswith="@dial.local").delete()
        # not cascaded from Event: audit log entries and PBX realtime/outbox rows
        AuditLog.objects.all().delete()
        PBXJob.objects.all().delete()
        VoicemailUser.objects.all().delete()
    # the in-process dummy PBX/DECT adapters remember the seeded subscriptions - drop them for later modules
    from apps.dect import reset_dect_cache
    from apps.pbx import reset_pbx_cache

    reset_dect_cache()
    reset_pbx_cache()


@pytest.mark.django_db
@pytest.mark.parametrize(("who", "url"), CASES, ids=[f"{w}:{u}" for w, u in CASES])
def test_page_has_no_a11y_findings(demo, who, url):
    client = Client()
    if demo["users"][who] is not None:
        client.force_login(demo["users"][who])
    findings = audit_url(client, url.format(**demo["fmt"]))
    if findings is None:
        pytest.skip("not an HTML 200 response")
    assert findings == [], "\n" + "\n".join(findings)
