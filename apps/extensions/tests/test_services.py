import pytest

from apps.extensions import services
from apps.extensions.models import Extension, ExtensionType
from apps.numbering.models import NumberRange

pytestmark = pytest.mark.django_db


def test_policy_lengths(event, user):
    plan = services.get_plan(event)
    assert not plan.evaluate("123", user=user).allowed
    assert plan.evaluate("4242", user=user).allowed
    assert not plan.evaluate("42424", user=user).allowed


def test_policy_blocked_and_emergency(event, user):
    plan = services.get_plan(event)
    assert not plan.evaluate("0123", user=user).allowed
    assert not plan.evaluate("9000", user=user).allowed
    assert not plan.evaluate("112", user=user).allowed


def test_policy_restricted_roles(event, user, orga):
    plan = services.get_plan(event)
    assert not plan.evaluate("1234", user=user).allowed
    assert plan.evaluate("1234", user=orga).allowed


def test_policy_restricted_group(event, user, member, angels):
    plan = services.get_plan(event)
    rng = NumberRange.objects.get(name="Angels")
    rng.allowed_groups.add(angels)
    assert not plan.evaluate("2323", user=user).allowed
    member.groups.add(angels)
    assert plan.evaluate("2323", user=user).allowed


def test_vanity_requires_approval(event, user):
    plan = services.get_plan(event)
    res = plan.evaluate("4444", user=user)
    assert res.allowed and res.requires_approval


def test_register_instant_and_approval_flow(event, user, orga):
    ext = services.register(event, user, "4242", ExtensionType.DECT)
    assert ext.state == Extension.State.ACTIVE
    ext2 = services.register(event, user, "5555", ExtensionType.SIP)
    assert ext2.state == Extension.State.REQUESTED
    services.approve(ext2, orga, "ok")
    ext2.refresh_from_db()
    assert ext2.state == Extension.State.ACTIVE
    assert ext2.moderated_by == orga


def test_register_taken(event, user, other_user):
    services.register(event, user, "4242", ExtensionType.DECT)
    with pytest.raises(services.ExtensionError):
        services.register(event, other_user, "4242", ExtensionType.DECT)
    av = services.check_availability(event, "4242", user=other_user)
    assert av.taken and not av.available
    assert av.suggestions and "4242" not in av.suggestions


def test_quota(event, user):
    event.max_extensions_per_user = 1
    event.save()
    services.register(event, user, "4242", ExtensionType.DECT)
    with pytest.raises(services.ExtensionError):
        services.register(event, user, "4243", ExtensionType.DECT)


def test_port_between_events(event, user, db):
    import datetime as dt


    ext = services.register(event, user, "4242", ExtensionType.DECT, display_name="Alice")
    new = event.clone(name="Demo 2", slug="demo2", start_date=dt.date.today(), end_date=dt.date.today())
    new.transition("registration")
    assert new.number_plan.ranges.count() == event.number_plan.ranges.count()
    portable = services.portable_extensions(user, new)
    assert [e.number for e in portable] == ["4242"]
    ported = services.port(ext, new, user)
    assert ported.event == new and ported.ported_from == ext and ported.display_name == "Alice"


def test_transfer(event, user, other_user):
    ext = services.register(event, user, "4242", ExtensionType.DECT)
    tr = services.start_transfer(ext, user, other_user)
    services.accept_transfer(tr, other_user)
    ext.refresh_from_db()
    assert ext.owner == other_user


def test_guest_extension_claim(event, admin, user):
    ext = services.create_guest_extension(event, "7777", admin)
    assert ext.claim_token and ext.state == Extension.State.SUSPENDED
    claimed = services.claim_guest_extension(ext.claim_token, user)
    assert claimed.owner == user and claimed.state == Extension.State.ACTIVE


def test_audit_log_written(event, user):
    from apps.core.models import AuditLog

    services.register(event, user, "4242", ExtensionType.DECT)
    assert AuditLog.objects.filter(action="create", event=event).exists()


def test_export_import_roundtrip(event, user, angels, member):
    from apps.events.export import export_event, import_event

    services.register(event, user, "4242", ExtensionType.DECT)
    data = export_event(event)
    new = import_event(data, slug_override="demo-copy")
    assert new.extensions.count() == 1
    assert new.groups.count() == 1
    assert new.number_plan.ranges.count() == 5
