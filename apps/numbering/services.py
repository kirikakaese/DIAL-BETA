"""Numbering services: free-number discovery (pools, random numbers) and extension claims.

Availability itself lives in ``apps.extensions.services`` (``check_availability`` / ``taken_info``); the
helpers here build on it for bulk scans and orga pre-reservations.
"""
from __future__ import annotations

import random
from bisect import bisect_left
from collections.abc import Iterable

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.audit import log
from apps.extensions.models import Extension, ExtensionType
from apps.extensions.services import (
    LIVE_STATES,
    ExtensionError,
    check_availability,
    get_plan,
    register,
)

from .models import ExtensionClaim, ExtensionPool, NumberPlan

# Upper bound on candidates inspected by a sequential scan without pools (min_length=5 would be 100k numbers)
SCAN_LIMIT = 20000


# --- free numbers -----------------------------------------------------------------


class Blockers:
    """In-memory index of every number that occupies ``event``: live extensions, open claims (except the
    exact numbers claimed for ``user``) and the plan's own digit-only numbers. Answers "does this candidate
    clash?" without a query per candidate, honouring prefix-free numbering via a sorted list + bisect."""

    def __init__(self, event, plan: NumberPlan, user=None):
        self.prefix_free = plan.prefix_free
        live = set(Extension.objects.filter(event=event, state__in=LIVE_STATES).values_list("number", flat=True))
        own: set[str] = set()
        claimed: set[str] = set()
        for claim in ExtensionClaim.objects.open().filter(event=event):
            claimed.add(claim.number)
            if user is not None and claim.is_for(user):
                own.add(claim.number)
        # trunk blocks occupy every number of the block: (block prefix, number length)
        self.trunk_blocks = [(t.block_prefix, len(t.number))
                             for t in Extension.objects.filter(event=event, type=ExtensionType.TRUNK,
                                                               state__in=LIVE_STATES)]
        trunk_prefixes = {p for p, _n in self.trunk_blocks}
        self.exact = live | (claimed - own)
        self.all = live | claimed | set(plan.reserved_numbers())
        if self.prefix_free:
            self.exact |= trunk_prefixes
            self.all |= trunk_prefixes
        self.sorted = sorted(self.all)

    def blocks(self, number: str) -> bool:
        if number in self.exact:
            return True
        if any(len(number) == n and number.startswith(p) for p, n in self.trunk_blocks):
            return True
        if not self.prefix_free:
            return False
        if any(number[:i] in self.all for i in range(1, len(number))):
            return True
        idx = bisect_left(self.sorted, number)
        # everything that starts with ``number`` sorts directly after it
        while idx < len(self.sorted) and self.sorted[idx].startswith(number):
            if self.sorted[idx] != number:
                return True
            idx += 1
        return False


def _registrable(plan: NumberPlan, blockers: Blockers, number: str, user, extension_type) -> bool:
    policy = plan.evaluate(number, user=user, extension_type=extension_type)
    return policy.allowed and not policy.requires_approval and not blockers.blocks(number)


def free_numbers_in(event, candidates: Iterable[str], *, limit: int | None = None, user=None,
                    extension_type=None) -> list[str]:
    """Filter ``candidates`` down to numbers that could be registered instantly right now."""
    plan = get_plan(event).cache_ranges()
    blockers = Blockers(event, plan, user=user)
    out: list[str] = []
    for n in candidates:
        if _registrable(plan, blockers, n, user, extension_type):
            out.append(n)
            if limit is not None and len(out) >= limit:
                break
    return out


def active_pools(event):
    return list(ExtensionPool.objects.filter(event=event, is_active=True))


def _default_range_candidates(plan: NumberPlan):
    """Ascending numbers of the plan's default lengths (used when the event has no pools)."""
    seen = 0
    for length in range(plan.min_length, plan.max_length + 1):
        for i in range(10 ** length):
            if seen >= SCAN_LIMIT:
                return
            seen += 1
            yield f"{i:0{length}d}"


def random_free_number(event, user=None, extension_type=None, tries: int = 200) -> str | None:
    """A random number that ``user`` could register instantly, drawn from the event's active pools or -
    without pools - from the plan's default length range. ``None`` if nothing free turned up in ``tries``."""
    plan = get_plan(event).cache_ranges()
    blockers = Blockers(event, plan, user=user)
    pools = active_pools(event)
    weights = [p.size for p in pools]
    for _try in range(tries):
        if pools:
            n = random.choices(pools, weights=weights)[0].random_candidate()  # noqa: S311 - not security relevant
        else:
            length = random.randint(plan.min_length, plan.max_length)
            n = f"{random.randrange(10 ** length):0{length}d}"
        if _registrable(plan, blockers, n, user, extension_type):
            return n
    return None


def all_free_numbers(event, limit: int = 50, user=None, extension_type=None) -> list[str]:
    """Up to ``limit`` instantly registrable numbers, pool by pool (or from the default range without pools)."""
    pools = active_pools(event)
    if not pools:
        return free_numbers_in(event, _default_range_candidates(get_plan(event)), limit=limit, user=user,
                               extension_type=extension_type)
    out: list[str] = []
    for pool in pools:
        out += pool.free_numbers(limit - len(out), user=user, extension_type=extension_type)
        if len(out) >= limit:
            break
    return out


# --- claims ---------------------------------------------------------------------


def find_user_by_email(email: str):
    from django.contrib.auth import get_user_model

    if not email:
        return None
    return get_user_model().objects.filter(email__iexact=email.strip()).first()


def claim_redeem_url(claim: ExtensionClaim) -> str:
    path = reverse("numbering:claim_redeem", args=[claim.event.slug, claim.token])
    return settings.DIAL_PUBLIC_URL.rstrip("/") + path


def send_claim_invite(claim: ExtensionClaim) -> bool:
    """Mail the redeem link to the claimant. Returns False if there is no address to send to."""
    to = claim.invite_email
    if not to:
        return False
    event = claim.event
    body = _(
        "Hi,\n\nthe orga team of %(event)s reserved the extension %(number)s for you.\n"
        "Claim it here (valid until %(until)s):\n%(url)s\n\n%(note)s"
    ) % {
        "event": event.name, "number": claim.number,
        "until": timezone.localtime(claim.valid_until).strftime("%Y-%m-%d %H:%M"),
        "url": claim_redeem_url(claim), "note": claim.note,
    }
    send_mail(f"[DIAL] Extension {claim.number} reserved for you at {event.name}", body.rstrip() + "\n",
              settings.DEFAULT_FROM_EMAIL, [to], fail_silently=True)
    return True


@transaction.atomic
def create_claim(event, number: str, actor, *, user=None, email: str = "", type=ExtensionType.DECT,
                 valid_until=None, note: str = "", send_email: bool = True, request=None) -> ExtensionClaim:
    """Reserve ``number`` for ``user`` (or the person behind ``email``) and send them the invite link.

    The number must be free for the claimant (taken/prefix conflicts are never overridable); soft policy
    denials (length, restricted range, ...) are lifted because the orga vouches for the claimant.
    """
    number = (number or "").strip()
    email = (email or "").strip()
    if user is None and email:
        user = find_user_by_email(email)
    if user is None and not email:
        raise ExtensionError(_("A claim needs a user or an e-mail address to be reserved for."))
    if type not in ExtensionType.values:
        raise ExtensionError(_("Unknown extension type."))
    av = check_availability(event, number, user=user, extension_type=type, with_suggestions=False)
    if av.taken:
        raise ExtensionError(av.reason)
    if not av.policy.allowed and not av.policy.overridable:
        raise ExtensionError(av.policy.reason)
    if valid_until is not None and valid_until <= timezone.now():
        raise ExtensionError(_("The validity must end in the future."))
    claim = ExtensionClaim(event=event, number=number, type=type, user=user, email="" if user else email,
                           note=note, created_by=actor)
    if valid_until is not None:
        claim.valid_until = valid_until
    claim.save()
    log(action="create", actor=actor, target=claim, event=event, request=request,
        message=f"Extension {number} reserved for {claim.claimant_label}")
    if send_email:
        send_claim_invite(claim)
    return claim


@transaction.atomic
def redeem_claim(token: str, user, request=None) -> Extension:
    """Turn an open claim into an ACTIVE extension owned by ``user`` (must be the claimant)."""
    claim = ExtensionClaim.objects.select_related("event").filter(token=token).first()
    if claim is None:
        raise ExtensionError(_("Invalid claim link."))
    if claim.is_redeemed:
        raise ExtensionError(_("This claim has already been redeemed."))
    if not claim.is_open:
        raise ExtensionError(_("This claim has expired."))
    if not claim.is_for(user):
        raise ExtensionError(_("This claim is reserved for somebody else."))
    from apps.events.models import EventMembership

    EventMembership.objects.get_or_create(event=claim.event, user=user)
    ext = register(claim.event, user, claim.number, claim.type, request=request, force_active=True,
                   policy_override=True, request_note=claim.note)
    claim.redeemed_at = timezone.now()
    claim.redeemed_extension = ext
    if not claim.user_id:
        claim.user = user
    claim.save(update_fields=["redeemed_at", "redeemed_extension", "user", "updated_at"])
    log(action="update", actor=user, target=claim, event=claim.event, request=request,
        message=f"Claim for {claim.number} redeemed")
    return ext


def delete_claim(claim: ExtensionClaim, actor, request=None) -> None:
    log(action="delete", actor=actor, event=claim.event, request=request,
        message=f"Claim for {claim.number} ({claim.claimant_label}) deleted")
    claim.delete()


def expire_claims(event=None) -> int:
    """Claims expire implicitly (``valid_until`` is compared at read time); nothing to persist.

    Kept as a hook so a periodic task can prune stale claims later. Returns the number of expired claims.
    """
    qs = ExtensionClaim.objects.filter(redeemed_at__isnull=True, valid_until__lte=timezone.now())
    if event is not None:
        qs = qs.filter(event=event)
    return qs.count()
