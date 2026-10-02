"""Shared pytest fixtures."""
import datetime as dt

import pytest

from apps.accounts.models import User
from apps.events.models import Event, EventMembership, UserGroup
from apps.numbering.models import NumberPlan, NumberRange


@pytest.fixture
def user(db):
    return User.objects.create_user(email="alice@example.org", username="alice", password="pw-alice-1234")


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email="bob@example.org", username="bob", password="pw-bob-123456")


@pytest.fixture
def admin(db):
    return User.objects.create_superuser(email="root@example.org", username="root", password="pw-root-12345")


@pytest.fixture
def event(db):
    today = dt.date.today()
    ev = Event.objects.create(
        name="Demo Camp", slug="demo", state=Event.State.REGISTRATION,
        start_date=today, end_date=today + dt.timedelta(days=5), sip_domain="demo.pet.local",
        allow_guest_extensions=True,
    )
    plan = NumberPlan.objects.create(event=ev, min_length=4, max_length=4, test_ringback_number="9000",
                                     wakeup_service_number="9001", site_survey_number="9002",
                                     echo_test_number="9003", voicemail_number="9999",
                                     emergency_numbers=["112", "110", "911"])
    NumberRange.objects.create(plan=plan, name="Orga", prefix="1", mode="restricted",
                               allowed_roles=["orga", "admin"], priority=10)
    NumberRange.objects.create(plan=plan, name="Angels", prefix="2", mode="restricted", priority=20)
    NumberRange.objects.create(plan=plan, name="Vanity", pattern=r"(\d)\1{3}", is_vanity=True, priority=5)
    NumberRange.objects.create(plan=plan, name="Trunk prefix", prefix="0", mode="blocked", priority=1)
    NumberRange.objects.create(plan=plan, name="Services", prefix="9", mode="blocked", priority=1)
    return ev


@pytest.fixture
def angels(event):
    return UserGroup.objects.create(event=event, name="Angels", slug="angels")


@pytest.fixture
def orga(event, db):
    u = User.objects.create_user(email="orga@example.org", username="orga", password="pw-orga-12345")
    EventMembership.objects.create(event=event, user=u, role="orga")
    return u


@pytest.fixture
def member(event, user):
    return EventMembership.objects.create(event=event, user=user, role="user")
