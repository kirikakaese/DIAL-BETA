"""Prefix-free numbering, claims and orga overrides as seen through ``apps.extensions.services``."""
import pytest
from django.utils import timezone

from apps.extensions import services
from apps.extensions.models import Extension, ExtensionType
from apps.numbering import services as numbering
from apps.numbering.models import ExtensionClaim

pytestmark = pytest.mark.django_db


@pytest.fixture
def variable_plan(event):
    plan = services.get_plan(event)
    plan.min_length, plan.max_length = 2, 6
    plan.save()
    return plan


# --- prefix-free conflicts ------------------------------------------------------------


def test_short_number_blocks_longer_one(event, user, other_user, variable_plan):
    services.register(event, user, "42", ExtensionType.DECT)
    av = services.check_availability(event, "4242", user=other_user)
    assert av.taken and not av.available
    assert av.conflicts == ["42"]
    assert "42" in av.reason and "prefix" in av.reason
    assert av.as_dict()["conflicts"] == ["42"]
    with pytest.raises(services.ExtensionError, match="prefix"):
        services.register(event, other_user, "4242", ExtensionType.DECT)


def test_long_number_blocks_its_prefix(event, user, other_user, variable_plan):
    services.register(event, user, "4242", ExtensionType.DECT)
    av = services.check_availability(event, "42", user=other_user)
    assert av.taken and av.conflicts == ["4242"]
    # unrelated numbers stay free
    assert services.check_availability(event, "43", user=other_user).available
    assert services.check_availability(event, "4243", user=other_user).available


def test_conflicts_list_all_related_numbers(event, user, other_user, variable_plan):
    services.register(event, user, "4242", ExtensionType.DECT)
    services.register(event, user, "4243", ExtensionType.DECT)
    av = services.check_availability(event, "424", user=other_user)
    assert av.conflicts == ["4242", "4243"]


def test_conflicting_extensions_queryset_and_exclude(event, user, variable_plan):
    ext = services.register(event, user, "42", ExtensionType.DECT)
    qs = services.conflicting_extensions(event, "4242")
    assert list(qs) == [ext]
    assert not services.conflicting_extensions(event, "4242", exclude=ext).exists()
    # dead extensions never conflict
    ext.state = Extension.State.DELETED
    ext.save()
    assert not services.is_taken(event, "4242")


def test_service_numbers_and_feature_codes_count_as_prefixes(event, orga, variable_plan):
    variable_plan.callback_request_code = "55"  # digit-only feature code
    variable_plan.save()
    assert "55" in variable_plan.reserved_numbers()
    assert "*86" not in variable_plan.reserved_numbers()
    av = services.check_availability(event, "5500", user=orga)
    assert av.taken and av.conflicts == ["55"]
    # emergency number 112 blocks 1120 (orga may otherwise use the 1xxx range)
    av = services.check_availability(event, "1120", user=orga)
    assert av.taken and av.conflicts == ["112"]
    # the exact service number is still a policy denial, not a conflict
    assert services.check_availability(event, "112", user=orga).policy.code == "emergency"


def test_prefix_free_off_restores_exact_match_only(event, user, other_user, variable_plan):
    variable_plan.prefix_free = False
    variable_plan.save()
    services.register(event, user, "42", ExtensionType.DECT)
    av = services.check_availability(event, "4242", user=other_user)
    assert av.available and not av.conflicts
    assert services.check_availability(event, "42", user=other_user).taken
    assert variable_plan.prefix_conflicts("1120") == []


def test_suggestions_respect_prefix_free(event, user, other_user, variable_plan):
    services.register(event, user, "43", ExtensionType.DECT)  # blocks every 43xx
    services.register(event, user, "4299", ExtensionType.DECT)
    av = services.check_availability(event, "4299", user=other_user)
    assert av.suggestions
    assert all(not s.startswith("43") for s in av.suggestions)
    assert all(not services.is_taken(event, s) for s in av.suggestions)


def test_orga_force_active_bypasses_length_but_never_prefix(event, user, orga):
    with pytest.raises(services.ExtensionError):
        services.register(event, user, "424", ExtensionType.DECT, force_active=True)
    ext = services.register(event, orga, "424", ExtensionType.DECT, force_active=True)
    assert ext.state == Extension.State.ACTIVE
    with pytest.raises(services.ExtensionError, match="prefix"):
        services.register(event, orga, "4240", ExtensionType.DECT, force_active=True)
    # hard denials stay hard
    with pytest.raises(services.ExtensionError):
        services.register(event, orga, "112", ExtensionType.DECT, force_active=True, policy_override=True)


# --- claims -----------------------------------------------------------------------------


def test_claim_blocks_others_but_not_claimant_by_user(event, user, other_user, orga):
    claim = numbering.create_claim(event, "4242", orga, user=user, send_email=False)
    assert claim.is_open and claim.user == user
    av = services.check_availability(event, "4242", user=other_user)
    assert av.taken and av.reserved and "reserved for another user" in av.reason
    with pytest.raises(services.ExtensionError, match="reserved"):
        services.register(event, other_user, "4242", ExtensionType.DECT)
    assert services.check_availability(event, "4242", user=user).available
    # anonymous callers see it as taken
    assert services.check_availability(event, "4242").taken


def test_claim_by_email_matches_case_insensitively(event, other_user, orga, django_user_model):
    claim = numbering.create_claim(event, "4242", orga, email="Carol@Example.org", send_email=False)
    assert claim.user is None and claim.email == "Carol@Example.org"
    carol = django_user_model.objects.create_user(email="carol@example.org", username="carol", password="pw-carol-1234")
    assert claim.is_for(carol) and not claim.is_for(other_user)
    assert services.check_availability(event, "4242", user=carol).available
    assert services.check_availability(event, "4242", user=other_user).reserved


def test_claim_email_resolves_existing_account(event, user, orga):
    claim = numbering.create_claim(event, "4242", orga, email="ALICE@example.org", send_email=False)
    assert claim.user == user and claim.email == ""


def test_claim_participates_in_prefix_conflicts(event, user, other_user, orga, variable_plan):
    numbering.create_claim(event, "42", orga, user=user, send_email=False)
    av = services.check_availability(event, "4242", user=other_user)
    assert av.taken and av.conflicts == ["42"]
    # a claim cannot be placed on a conflicting number either
    services.register(event, other_user, "5555", ExtensionType.DECT)
    with pytest.raises(services.ExtensionError):
        numbering.create_claim(event, "55", orga, user=user, send_email=False)


def test_claim_requires_free_number_and_claimant(event, user, other_user, orga):
    services.register(event, other_user, "4242", ExtensionType.DECT)
    with pytest.raises(services.ExtensionError):
        numbering.create_claim(event, "4242", orga, user=user, send_email=False)
    with pytest.raises(services.ExtensionError):
        numbering.create_claim(event, "4243", orga, send_email=False)
    with pytest.raises(services.ExtensionError):  # emergency numbers are never claimable
        numbering.create_claim(event, "112", orga, user=user, send_email=False)


def test_redeem_creates_active_extension(event, user, orga, mailoutbox, settings):
    settings.DIAL_PUBLIC_URL = "https://dial.example"
    claim = numbering.create_claim(event, "1234", orga, user=user, note="Infodesk")  # orga-only range
    assert len(mailoutbox) == 1 and claim.token in mailoutbox[0].body
    assert f"https://dial.example/e/{event.slug}/numbering/claim/{claim.token}/" in mailoutbox[0].body
    ext = numbering.redeem_claim(claim.token, user)
    assert ext.state == Extension.State.ACTIVE and ext.owner == user and ext.number == "1234"
    assert ext.request_note == "Infodesk"
    claim.refresh_from_db()
    assert claim.is_redeemed and claim.redeemed_extension == ext and not claim.is_open
    assert event.memberships.filter(user=user).exists()
    with pytest.raises(services.ExtensionError, match="already"):
        numbering.redeem_claim(claim.token, user)


def test_redeem_by_wrong_user_or_expired_fails(event, user, other_user, orga):
    claim = numbering.create_claim(event, "4242", orga, user=user, send_email=False)
    with pytest.raises(services.ExtensionError, match="somebody else"):
        numbering.redeem_claim(claim.token, other_user)
    ExtensionClaim.objects.filter(pk=claim.pk).update(valid_until=timezone.now() - timezone.timedelta(minutes=1))
    with pytest.raises(services.ExtensionError, match="expired"):
        numbering.redeem_claim(claim.token, user)
    # an expired claim no longer blocks the number
    assert services.check_availability(event, "4242", user=other_user).available
    assert numbering.expire_claims(event) == 1
    with pytest.raises(services.ExtensionError):
        numbering.redeem_claim("nope", user)
