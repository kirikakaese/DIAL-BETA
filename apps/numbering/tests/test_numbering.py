"""Extension pools, random free numbers and the numbering portal pages."""
import pytest
from django.urls import reverse
from django.utils import timezone

from apps.extensions import services as ext_services
from apps.extensions.models import Extension, ExtensionType
from apps.numbering import services
from apps.numbering.models import ExtensionClaim, ExtensionPool

pytestmark = pytest.mark.django_db


@pytest.fixture
def pool(event):
    return ExtensionPool.objects.create(event=event, name="Hackcenter", prefix="42", length=4)


# --- pools ---------------------------------------------------------------------------------


def test_pool_candidates_and_free_numbers(event, user, pool):
    cands = list(pool.candidates())
    assert len(cands) == pool.size == 100 and cands[0] == "4200" and cands[-1] == "4299"
    assert pool.mask == "42xx"
    ext_services.register(event, user, "4200", ExtensionType.DECT)
    free = pool.free_numbers()
    assert "4200" not in free
    assert "4242" in free
    assert len(free) == 99
    assert pool.free_numbers(limit=5) == ["4201", "4202", "4203", "4204", "4205"]


def test_pool_free_numbers_skip_policy_denials_approval_and_claims(event, user, other_user, orga):
    pool = ExtensionPool.objects.create(event=event, name="Fives", prefix="5", length=4)
    services.create_claim(event, "5100", orga, user=other_user, send_email=False)
    free = pool.free_numbers(user=user)
    assert "5555" not in free  # vanity -> approval
    assert "5100" not in free  # claimed for somebody else
    assert "5100" in pool.free_numbers(user=other_user)  # ... but free for the claimant
    assert len(free) == 998


def test_pool_free_numbers_honour_prefix_free(event, user, pool):
    plan = ext_services.get_plan(event)
    plan.min_length = 2
    plan.save()
    ext_services.register(event, user, "421", ExtensionType.DECT)
    free = pool.free_numbers()
    assert not any(n.startswith("421") for n in free) and len(free) == 90
    plan.prefix_free = False
    plan.save()
    assert len(pool.free_numbers()) == 100


def test_random_free_number_from_pool_is_registrable(event, user, pool):
    n = services.random_free_number(event, user=user)
    assert n and n.startswith("42") and len(n) == 4
    ext = ext_services.register(event, user, n, ExtensionType.DECT)
    assert ext.state == Extension.State.ACTIVE
    # inactive pools are ignored; exhausted pools yield None
    pool.is_active = False
    pool.save()
    ExtensionPool.objects.create(event=event, name="Tiny", prefix="777", length=4)  # 7770-7779, 7777 is vanity
    Extension.objects.bulk_create([
        Extension(event=event, number=f"777{i}", state=Extension.State.ACTIVE) for i in range(10) if i != 7
    ])
    assert services.random_free_number(event, user=user, tries=50) is None


def test_random_free_number_without_pools_uses_default_range(event, user):
    n = services.random_free_number(event, user=user)
    assert n and len(n) == 4 and n[0] not in "0129"
    ext = ext_services.register(event, user, n, ExtensionType.DECT)
    assert ext.state == Extension.State.ACTIVE


def test_all_free_numbers(event, user, pool):
    ExtensionPool.objects.create(event=event, name="Sixes", prefix="6", length=4)
    out = services.all_free_numbers(event, limit=120)
    assert len(out) == 120 and out[0] == "4200" and out[100] == "6000"
    ExtensionPool.objects.all().delete()
    out = services.all_free_numbers(event, limit=3)
    assert len(out) == 3 and all(len(n) == 4 for n in out)


# --- portal views ----------------------------------------------------------------------------


def test_orga_pages_require_orga(client, event, user, member):
    client.force_login(user)
    for name in ("numbering:settings", "numbering:pools", "numbering:claims"):
        assert client.get(reverse(name, args=[event.slug])).status_code == 403


def test_settings_view_toggles_prefix_free(client, event, orga):
    client.force_login(orga)
    url = reverse("numbering:settings", args=[event.slug])
    assert client.get(url).status_code == 200
    resp = client.post(url, {})  # unchecked checkbox -> False
    assert resp.status_code == 302
    assert ext_services.get_plan(event).prefix_free is False
    client.post(url, {"prefix_free": "on"})
    assert ext_services.get_plan(event).prefix_free is True


def test_pools_view_create_and_delete(client, event, orga):
    client.force_login(orga)
    url = reverse("numbering:pools", args=[event.slug])
    assert client.get(url).status_code == 200
    resp = client.post(url, {"name": "Angels", "prefix": "2", "length": 4, "is_active": "on", "description": ""})
    assert resp.status_code == 302
    pool = ExtensionPool.objects.get(event=event, name="Angels")
    assert client.get(url).status_code == 200
    # invalid: prefix not shorter than length
    resp = client.post(url, {"name": "Bad", "prefix": "1234", "length": 4})
    assert resp.status_code == 200 and ExtensionPool.objects.filter(name="Bad").count() == 0
    resp = client.post(reverse("numbering:pool_delete", args=[event.slug, pool.pk]))
    assert resp.status_code == 302 and not ExtensionPool.objects.filter(pk=pool.pk).exists()


def test_random_endpoint(client, event, user, member, pool):
    url = reverse("numbering:random", args=[event.slug])
    assert client.get(url).status_code == 302  # login required
    client.force_login(user)
    data = client.get(url + "?type=dect").json()
    assert data["number"].startswith("42")


def test_api_random_number(client, event, user, member, pool):
    client.force_login(user)
    resp = client.get(f"/api/v1/random-number/?event={event.slug}&type=sip")
    assert resp.status_code == 200 and resp.json()["number"].startswith("42")


def test_claims_view_list_create_resend_delete(client, event, orga, user, mailoutbox):
    client.force_login(orga)
    url = reverse("numbering:claims", args=[event.slug])
    assert client.get(url).status_code == 200
    resp = client.post(url, {"number": "4242", "email": "alice@example.org", "type": "dect", "valid_until": "",
                             "note": "Infodesk", "send_email": "on"})
    assert resp.status_code == 302
    claim = ExtensionClaim.objects.get(event=event, number="4242")
    assert claim.user == user and claim.is_open and len(mailoutbox) == 1
    assert claim.valid_until > timezone.now() + timezone.timedelta(days=13)
    page = client.get(url)
    assert page.status_code == 200 and b"4242" in page.content
    # duplicate -> form error, no second claim
    resp = client.post(url, {"number": "4242", "email": "bob@example.org", "type": "dect", "send_email": "on"})
    assert resp.status_code == 200 and ExtensionClaim.objects.filter(number="4242").count() == 1
    assert client.post(reverse("numbering:claim_resend", args=[event.slug, claim.pk])).status_code == 302
    assert len(mailoutbox) == 2
    assert client.post(reverse("numbering:claim_delete", args=[event.slug, claim.pk])).status_code == 302
    assert not ExtensionClaim.objects.filter(pk=claim.pk).exists()


def test_claim_redeem_view(client, event, orga, user, other_user):
    claim = services.create_claim(event, "4242", orga, user=user, send_email=False)
    url = reverse("numbering:claim_redeem", args=[event.slug, claim.token])
    assert client.get(url).status_code == 302  # login required
    client.force_login(other_user)
    page = client.get(url)
    assert page.status_code == 200 and b"reserved for" in page.content
    resp = client.post(url)
    assert resp.status_code == 302 and resp.url == url  # error message, stays on page
    client.force_login(user)
    assert client.get(url).status_code == 200
    resp = client.post(url)
    ext = Extension.objects.get(event=event, number="4242")
    assert resp.status_code == 302 and resp.url == reverse("portal:extension_detail", args=[event.slug, ext.pk])
    assert ext.owner == user and ext.is_active
    assert client.get(reverse("numbering:claim_redeem", args=[event.slug, "bogus"])).status_code == 404
