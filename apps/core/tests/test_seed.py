"""``seed_demo`` must produce a complete, idempotent demo event on a fresh database."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

pytestmark = pytest.mark.django_db(transaction=False)


def _seed(*args):
    out = StringIO()
    call_command("seed_demo", *args, stdout=out, verbosity=1)
    return out.getvalue()


def test_seed_demo_creates_event_and_extensions():
    from apps.accounts.models import User
    from apps.callgroups.models import CallGroup
    from apps.dect.models import RFP, VenueMap
    from apps.devices.models import Device
    from apps.events.models import Event, UserGroup
    from apps.extensions.models import Extension
    from apps.stats.models import CallRecord, HourlyStat
    from apps.voicemail.models import Mailbox

    out = _seed()
    assert "Demo data for 'demo' is ready" in out
    assert "skipped" not in out, out  # every feature block succeeded

    event = Event.objects.get(slug="demo")
    assert event.state == Event.State.LIVE
    assert event.allow_guest_extensions
    admin = User.objects.get(email="admin@dial.local")
    assert admin.is_superuser and admin.check_password("admin")
    assert User.objects.get(email="alice@dial.local").check_password("demo1234!")
    assert set(UserGroup.objects.filter(event=event).values_list("slug", flat=True)) == {
        "angels",
        "medics",
        "orga",
    }

    plan = event.number_plan
    assert (plan.min_length, plan.max_length) == (4, 4)
    assert plan.emergency_numbers == ["112", "110"]
    assert plan.ranges.count() == 6

    exts = Extension.objects.filter(event=event)
    assert exts.count() >= 30
    assert exts.filter(state=Extension.State.ACTIVE, type__in=["dect", "sip"]).count() >= 24
    assert exts.filter(state=Extension.State.REQUESTED).count() == 4
    assert exts.filter(is_temporary=True, number__in=["7770", "7771"]).count() == 2
    assert exts.get(number="4000").type == "announcement"
    assert exts.get(number="5000").config["pin"] == "1234"
    assert not exts.exclude(provision_error="").exists()

    devices = Device.objects.filter(event=event)
    assert devices.filter(type="dect").count() >= 10
    assert devices.filter(type="sip").exclude(sip_password="").count() >= 10
    assert all(len(d.ipei) == 13 for d in devices.filter(type="dect"))
    assert devices.filter(type="dect", omm_ppn="").count() == 0
    assert devices.filter(type="dect", last_seen_rfp__isnull=True).count() == 0

    helpdesk = CallGroup.objects.get(event=event, extension__number="3000")
    assert helpdesk.strategy == "ringall" and helpdesk.members.filter(logged_in=True).count() == 3
    assert CallGroup.objects.get(event=event, extension__number="3001").strategy == "roundrobin"
    assert Mailbox.objects.filter(event=event).count() >= 3

    assert RFP.objects.filter(event=event).count() == 6
    assert (
        RFP.objects.filter(event=event, pos_x__isnull=False, venue_map__isnull=False).count() == 6
    )
    assert VenueMap.objects.filter(event=event, is_default=True).exists()

    assert CallRecord.objects.filter(event=event).count() == 300
    assert HourlyStat.objects.filter(event=event).exists()
    assert event.webhooks.filter(is_active=False, url="http://example.invalid/hook").exists()


def test_seed_demo_is_idempotent_and_resettable():
    from apps.devices.models import Device
    from apps.extensions.models import Extension
    from apps.stats.models import CallRecord

    _seed()
    n_ext, n_dev, n_cdr = (
        Extension.objects.count(),
        Device.objects.count(),
        CallRecord.objects.count(),
    )
    _seed()
    assert (Extension.objects.count(), Device.objects.count(), CallRecord.objects.count()) == (
        n_ext,
        n_dev,
        n_cdr,
    )

    out = _seed("--reset")
    assert "Resetting demo data" in out
    assert Extension.objects.filter(event__slug="demo", state="requested").count() == 4
    assert Extension.objects.count() == n_ext
