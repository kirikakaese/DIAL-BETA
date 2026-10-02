"""Extension business logic: availability, registration, moderation, transfer.

All state changes go through here so that policy checks, audit logging,
webhooks and PBX/DECT provisioning stay consistent across portal, API and CLI.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.audit import log
from apps.events.webhooks import emit
from apps.numbering.blocks import block_prefix, evaluate_block
from apps.numbering.models import ExtensionClaim, NumberPlan, PolicyResult

from .models import TRUNK_BLOCK_DIGITS, Extension, ExtensionRequest, ExtensionTransfer, ExtensionType


class ExtensionError(Exception):
    pass


@dataclass
class Availability:
    number: str
    available: bool
    taken: bool
    policy: PolicyResult
    quota_ok: bool
    suggestions: list[str]
    # Live numbers / plan numbers that clash with this one under prefix-free numbering (exact matches excluded)
    conflicts: list[str] = field(default_factory=list)
    # An open claim holds this exact number for somebody else
    reserved: bool = False
    # Trunk blocks (``4700-4799``) that this number - or this block - overlaps with
    blocks: list[str] = field(default_factory=list)
    # > 0 when a whole trunk block based at ``number`` was checked
    block_digits: int = 0

    @property
    def requires_approval(self):
        return self.policy.requires_approval

    @property
    def reason(self) -> str:
        if self.taken:
            if self.blocks:
                return _("This number lies inside the trunk block %(blocks)s.") % {"blocks": ", ".join(self.blocks)}
            if self.conflicts and self.block_digits:
                return _("The block overlaps existing numbers: %(numbers)s.") % {"numbers": ", ".join(self.conflicts)}
            if self.conflicts:
                return _("Conflicts with existing extension %(numbers)s: numbers may not be a prefix of each other."
                         ) % {"numbers": ", ".join(self.conflicts)}
            if self.reserved:
                return _("This number is reserved for another user.")
            return _("This number is already taken.")
        if not self.policy.allowed:
            return self.policy.reason
        if not self.quota_ok:
            return _("You have reached your extension quota for this event.")
        return ""

    def as_dict(self):
        return {
            "number": self.number,
            "available": self.available,
            "taken": self.taken,
            "requires_approval": self.requires_approval,
            "reason": str(self.reason),
            "range": self.policy.range.name if self.policy.range else None,
            "suggestions": self.suggestions,
            "conflicts": self.conflicts,
            "reserved": self.reserved,
            "blocks": self.blocks,
            "block_digits": self.block_digits,
        }


def get_plan(event) -> NumberPlan:
    plan, _created = NumberPlan.objects.get_or_create(event=event)
    return plan


# --- availability --------------------------------------------------------------

LIVE_STATES = [Extension.State.REQUESTED, Extension.State.ACTIVE, Extension.State.SUSPENDED]


@dataclass
class TakenInfo:
    """Why a number counts as taken (see :func:`taken_info`)."""

    taken: bool
    conflicts: list[str] = field(default_factory=list)
    reserved: bool = False
    blocks: list[str] = field(default_factory=list)

    def __bool__(self):
        return self.taken


def _prefix_q(number: str, prefix_free: bool) -> Q:
    """Exact match, plus - under prefix-free numbering - every number that starts with ``number`` and every
    strict prefix of it (``number[:i]``)."""
    q = Q(number=number)
    if prefix_free and number:
        q |= Q(number__startswith=number)
        prefixes = [number[:i] for i in range(1, len(number))]
        if prefixes:
            q |= Q(number__in=prefixes)
    return q


def _block_q(number: str, block_digits: int, prefix_free: bool) -> Q:
    """Plain numbers that clash with ``number`` - or, for ``block_digits`` > 0, with the trunk block based at
    ``number``. A block behaves like its prefix (``47`` for ``4700``/2): under prefix-free numbering every
    number that is prefix-related to the block prefix clashes; otherwise only numbers inside the block."""
    if not block_digits:
        return _prefix_q(number, prefix_free)
    prefix = block_prefix(number, block_digits)
    inside = Q(number__regex=rf"^{prefix}[0-9]{{{block_digits}}}$")
    return (_prefix_q(prefix, True) | inside) if prefix_free else inside


def live_trunks(event, states=None):
    """Trunk extensions of ``event`` that occupy their block (live states by default)."""
    return Extension.objects.filter(event=event, type=ExtensionType.TRUNK, state__in=states or LIVE_STATES)


def _blocks_overlap(prefix_a: str, len_a: int, prefix_b: str, len_b: int, prefix_free: bool) -> bool:
    related = prefix_a.startswith(prefix_b) or prefix_b.startswith(prefix_a)
    return related and (prefix_free or len_a == len_b)


def overlapping_trunks(event, number: str, block_digits: int = 0, exclude: Extension | None = None,
                       plan: NumberPlan | None = None, states=None) -> list[Extension]:
    """Live trunks whose block contains ``number`` (or overlaps the block ``number``/``block_digits``)."""
    plan = plan or get_plan(event)
    prefix = block_prefix(number, block_digits)
    out = []
    for trunk in live_trunks(event, states):
        if exclude is not None and trunk.pk == exclude.pk:
            continue
        if _blocks_overlap(prefix, len(number), trunk.block_prefix, len(trunk.number), plan.prefix_free):
            out.append(trunk)
    return out


def trunk_for_number(event, number: str) -> Extension | None:
    """The *active* trunk whose block covers ``number`` (``None`` if there is none)."""
    number = (number or "").strip()
    if not number.isdigit():
        return None
    for trunk in live_trunks(event, [Extension.State.ACTIVE]).select_related("event"):
        if trunk.covers(number):
            return trunk
    return None


def conflicting_extensions(event, number: str, exclude: Extension | None = None, plan: NumberPlan | None = None,
                           block_digits: int = 0):
    """Live extensions that block ``number``: the exact number and, when the plan is prefix-free, any live
    number that is a strict prefix of it or has it as a prefix (``23`` vs. ``2323``). Numbers inside a live
    trunk block count as taken by that trunk; with ``block_digits`` the whole block based at ``number`` is
    checked instead of the single number."""
    plan = plan or get_plan(event)
    q = _block_q(number, block_digits, plan.prefix_free)
    trunks = overlapping_trunks(event, number, block_digits, exclude=exclude, plan=plan)
    if trunks:
        q |= Q(pk__in=[t.pk for t in trunks])
    qs = Extension.objects.filter(event=event, state__in=LIVE_STATES).filter(q)
    if exclude:
        qs = qs.exclude(pk=exclude.pk)
    return qs


def conflicting_claims(event, number: str, user=None, plan: NumberPlan | None = None, block_digits: int = 0):
    """Open claims that block ``number`` for ``user``: exact claims held for somebody else and, under
    prefix-free numbering, claims whose number is prefix-related to ``number``."""
    plan = plan or get_plan(event)
    qs = ExtensionClaim.objects.open().filter(event=event).filter(_block_q(number, block_digits, plan.prefix_free))
    if user is not None and getattr(user, "is_authenticated", True):
        own = Q(user=user)
        if user.email:
            own |= Q(user__isnull=True, email__iexact=user.email)
        qs = qs.exclude(Q(number=number) & own)
    return qs


def taken_info(event, number: str, exclude: Extension | None = None, user=None,
               plan: NumberPlan | None = None, block_digits: int = 0) -> TakenInfo:
    """Is ``number`` occupied - by a live extension, an open claim for someone else, or (prefix-free) by a
    prefix-related extension/claim/service number? ``user`` is the prospective owner; their own claims
    on the exact number do not count. ``block_digits`` checks a whole trunk block based at ``number``."""
    plan = plan or get_plan(event)
    exact = False
    reserved = False
    conflicts: set[str] = set()
    blocks: set[str] = set()
    for ext in conflicting_extensions(event, number, exclude=exclude, plan=plan, block_digits=block_digits):
        if ext.number == number and not (ext.is_trunk and block_digits != ext.block_digits):
            exact = True
        elif ext.is_trunk:
            blocks.add(ext.number_label)
        else:
            conflicts.add(ext.number)
    for n in conflicting_claims(event, number, user=user, plan=plan, block_digits=block_digits).values_list(
            "number", flat=True):
        if n == number:
            reserved = True
        else:
            conflicts.add(n)
    if block_digits:
        conflicts.update(plan.prefix_conflicts(block_prefix(number, block_digits)))
    else:
        conflicts.update(plan.prefix_conflicts(number))
    return TakenInfo(taken=exact or reserved or bool(conflicts) or bool(blocks), conflicts=sorted(conflicts),
                     reserved=reserved, blocks=sorted(blocks))


def is_taken(event, number: str, exclude: Extension | None = None, user=None,
             plan: NumberPlan | None = None) -> bool:
    return taken_info(event, number, exclude=exclude, user=user, plan=plan).taken


def quota_ok(event, user, rng=None) -> bool:
    if user is None or user.is_superuser:
        return True
    live = Extension.objects.filter(
        event=event, owner=user,
        state__in=[Extension.State.REQUESTED, Extension.State.ACTIVE, Extension.State.SUSPENDED],
    )
    if live.count() >= event.max_extensions_per_user and not user.is_orga(event):
        return False
    if rng is not None and rng.quota_per_user is not None:
        in_range = [e for e in live if rng.matches(e.number)]
        if len(in_range) >= rng.quota_per_user:
            return False
    return True


def suggest(event, number: str, limit: int = 5, user=None) -> list[str]:
    """Nearby free numbers, useful when the requested one is taken (respects prefix-free conflicts and claims)."""
    plan = get_plan(event).cache_ranges()
    out: list[str] = []
    if not number.isdigit():
        return out
    base = int(number)
    width = len(number)
    for delta in range(1, 200):
        for cand in (base + delta, base - delta):
            if cand < 0:
                continue
            s = str(cand).zfill(width)
            if len(s) != width:
                continue
            policy = plan.evaluate(s, user=user)
            if policy.allowed and not policy.requires_approval and not is_taken(event, s, user=user, plan=plan):
                out.append(s)
            if len(out) >= limit:
                return out
    return out


def check_availability(event, number: str, user=None, extension_type: str | None = None,
                       with_suggestions=True, block_digits: int = 0) -> Availability:
    """``block_digits`` > 0 (trunk registration) checks the whole block ``number``-``number[:-d]99..``:
    policy via :func:`evaluate_block`, occupancy via :func:`taken_info`."""
    number = (number or "").strip()
    plan = get_plan(event)
    block_digits = int(block_digits or 0)
    if block_digits:
        policy = evaluate_block(plan, number, block_digits, user=user, extension_type=extension_type or
                                ExtensionType.TRUNK)
    else:
        policy = plan.evaluate(number, user=user, extension_type=extension_type)
    info = (taken_info(event, number, user=user, plan=plan, block_digits=block_digits) if number
            else TakenInfo(taken=True))
    q_ok = quota_ok(event, user, policy.range) if policy.allowed else True
    available = bool(policy.allowed and not info.taken and q_ok and event.registration_open)
    suggestions = suggest(event, number, user=user) if (with_suggestions and info.taken and not block_digits) else []
    return Availability(number, available, info.taken, policy, q_ok, suggestions,
                        conflicts=info.conflicts, reserved=info.reserved, blocks=info.blocks,
                        block_digits=block_digits)


def trunk_block_digits(extension_type: str, fields: dict) -> int:
    """``config["block_digits"]`` of a trunk registration (validated), 0 for every other type."""
    if extension_type != ExtensionType.TRUNK:
        return 0
    raw = (fields.get("config") or {}).get("block_digits")
    try:
        digits = int(raw)
    except (TypeError, ValueError):
        raise ExtensionError(_("Choose the block size of the trunk (10, 100 or 1000 numbers)."))
    if digits not in TRUNK_BLOCK_DIGITS:
        raise ExtensionError(_("A trunk block has 10, 100 or 1000 numbers."))
    return digits


@transaction.atomic
def register(event, user, number: str, extension_type: str, *, request=None, ported_from=None,
             force_active=False, policy_override=False, **fields) -> Extension:
    """Self-service registration. Either activates instantly or creates a pending request.

    ``force_active`` (orga-driven flows) activates without moderation; an orga may additionally register
    numbers outside the plan's length limits. ``policy_override`` (claim redemption) lifts every soft
    denial (length, restricted range, type, closed default). Neither ever bypasses taken numbers, prefix
    conflicts, emergency/service numbers or blocked ranges.
    """
    if not event.registration_open and not (user and user.is_orga(event)):
        raise ExtensionError(_("Registration is not open for this event."))
    if (settings.DIAL_REQUIRE_EMAIL_VERIFICATION and user is not None and not force_active
            and not user.email_verified and not user.is_orga(event)):
        raise ExtensionError(_("Please verify your e-mail address before registering an extension."))
    if extension_type not in ExtensionType.values:
        raise ExtensionError(_("Unknown extension type."))
    block_digits = trunk_block_digits(extension_type, fields)
    if block_digits:
        fields["config"] = {**(fields.get("config") or {}), "block_digits": block_digits}
    av = check_availability(event, number, user=user, extension_type=extension_type,
                            with_suggestions=False, block_digits=block_digits)
    if av.taken:
        raise ExtensionError(av.reason)
    if not av.policy.allowed:
        orga_length_override = force_active and bool(user and user.is_orga(event)) and av.policy.code == "length"
        if not ((policy_override and av.policy.overridable) or orga_length_override):
            raise ExtensionError(av.policy.reason)
    if not av.quota_ok:
        raise ExtensionError(_("You have reached your extension quota for this event."))

    needs_approval = av.requires_approval and not force_active and not (user and user.is_orga(event))
    ext = Extension(
        event=event, owner=user, number=number, type=extension_type,
        state=Extension.State.REQUESTED if needs_approval else Extension.State.ACTIVE,
        ported_from=ported_from, **fields,
    )
    if not ext.display_name and user is not None:
        ext.display_name = user.username[:60]
    if ext.type == ExtensionType.DECT:
        ext.issue_dect_claim_code(save=False)
    elif ext.type == ExtensionType.ANNOUNCEMENT:
        ext.issue_record_code(save=False)
    try:
        ext.save()
    except IntegrityError:
        raise ExtensionError(_("This number was just taken by someone else."))
    log(action="create", actor=user, target=ext, event=event, request=request,
        message="Extension requested" if needs_approval else "Extension registered",
        changes={"state": [None, ext.state], "type": [None, ext.type]})
    emit("extension.created", _payload(ext), event=event)
    if ext.state == Extension.State.ACTIVE:
        _on_activated(ext)
    return ext


def _on_activated(ext: Extension):
    from apps.extensions.tasks import provision_extension

    ExtensionRequest.objects.filter(event=ext.event, number=ext.number).delete()
    provision_extension.delay(str(ext.pk))


@transaction.atomic
def approve(ext: Extension, moderator, note: str = "", request=None) -> Extension:
    if ext.state != Extension.State.REQUESTED:
        raise ExtensionError(_("Only requested extensions can be approved."))
    ext.state = Extension.State.ACTIVE
    ext.moderated_by = moderator
    ext.moderated_at = timezone.now()
    ext.moderation_note = note
    ext.save()
    log(action="approve", actor=moderator, target=ext, event=ext.event, request=request,
        message=note, changes={"state": ["requested", "active"]})
    emit("extension.approved", _payload(ext), event=ext.event)
    _on_activated(ext)
    return ext


@transaction.atomic
def reject(ext: Extension, moderator, note: str = "", request=None) -> Extension:
    if ext.state != Extension.State.REQUESTED:
        raise ExtensionError(_("Only requested extensions can be rejected."))
    ext.state = Extension.State.REJECTED
    ext.moderated_by = moderator
    ext.moderated_at = timezone.now()
    ext.moderation_note = note
    ext.save()
    log(action="reject", actor=moderator, target=ext, event=ext.event, request=request,
        message=note, changes={"state": ["requested", "rejected"]})
    emit("extension.rejected", _payload(ext), event=ext.event)
    return ext


@transaction.atomic
def suspend(ext: Extension, actor, note: str = "", request=None) -> Extension:
    old = ext.state
    ext.state = Extension.State.SUSPENDED
    ext.moderation_note = note
    ext.save()
    log(action="update", actor=actor, target=ext, request=request, message=note,
        changes={"state": [old, "suspended"]})
    from apps.extensions.tasks import deprovision_extension

    deprovision_extension.delay(str(ext.pk))
    return ext


@transaction.atomic
def reactivate(ext: Extension, actor, request=None) -> Extension:
    old = ext.state
    ext.state = Extension.State.ACTIVE
    ext.save()
    log(action="update", actor=actor, target=ext, request=request, changes={"state": [old, "active"]})
    _on_activated(ext)
    return ext


@transaction.atomic
def delete(ext: Extension, actor, request=None) -> Extension:
    old = ext.state
    # nobody may keep forwarding to a dead number (do it before save() so the audit log names the actor)
    clear_forwards_to(ext, actor=actor, request=request)
    ext.state = Extension.State.DELETED
    ext.save()
    log(action="delete", actor=actor, target=ext, request=request, changes={"state": [old, "deleted"]})
    emit("extension.deleted", _payload(ext), event=ext.event)
    from apps.extensions.tasks import deprovision_extension

    deprovision_extension.delay(str(ext.pk))
    _notify_waitlist(ext.event, ext.number)
    return ext


def update(ext: Extension, actor, request=None, **fields) -> Extension:
    changes = {}
    for k, v in fields.items():
        old = getattr(ext, k)
        if old != v:
            changes[k] = [old, v]
            setattr(ext, k, v)
    if changes:
        ext.save()
        log(action="update", actor=actor, target=ext, request=request, changes=changes)
        emit("extension.updated", _payload(ext), event=ext.event)
        if ext.is_active:
            from apps.extensions.tasks import provision_extension

            provision_extension.delay(str(ext.pk))
    return ext


def validate_device_binding(ext: Extension, device_type: str, device=None) -> None:
    """Raise :class:`ExtensionError` if a device of ``device_type`` may not be bound to ``ext``.

    Trunks carry exactly one SIP account (the remote PBX registers with it) - never DECT/GSM handsets.
    """
    if not ext.is_trunk:
        return
    if device_type != "sip":
        raise ExtensionError(_("A SIP trunk can only be bound to a SIP account."))
    bound = ext.bindings.all()
    if device is not None:
        bound = bound.exclude(device=device)
    if bound.exists():
        raise ExtensionError(_("A SIP trunk has exactly one SIP account. Detach the existing one first."))


def _notify_waitlist(event, number: str):
    from apps.extensions.tasks import notify_waitlist

    if ExtensionRequest.objects.filter(event=event, number=number).exists():
        notify_waitlist.delay(str(event.pk), number)


def join_waitlist(event, user, number: str, extension_type=ExtensionType.DECT) -> ExtensionRequest:
    if not is_taken(event, number):
        raise ExtensionError(_("This number is free - register it directly."))
    req, _created = ExtensionRequest.objects.get_or_create(
        event=event, user=user, number=number, defaults={"type": extension_type},
    )
    return req


# --- porting / cloning -------------------------------------------------------

def portable_extensions(user, target_event):
    """Extensions from other events that the user could re-request in ``target_event``."""
    taken = set(
        Extension.objects.filter(event=target_event, owner=user).exclude(
            state__in=[Extension.State.DELETED, Extension.State.REJECTED, Extension.State.EXPIRED]
        ).values_list("number", flat=True)
    )
    seen = set()
    out = []
    qs = (
        Extension.objects.filter(owner=user)
        .exclude(event=target_event)
        .exclude(state__in=[Extension.State.REJECTED, Extension.State.DELETED])
        .select_related("event")
        .order_by("-event__start_date")
    )
    for e in qs:
        if e.number in seen or e.number in taken:
            continue
        seen.add(e.number)
        out.append(e)
    return out


def port(ext: Extension, target_event, user, request=None) -> Extension:
    """Re-request the same number and settings in another event."""
    return register(
        target_event, user, ext.number, ext.type, request=request, ported_from=ext,
        display_name=ext.display_name, description=ext.description, in_phonebook=ext.in_phonebook,
        location_hint="", config=dict(ext.config), ring_strategy=ext.ring_strategy,
    )


# --- transfer ------------------------------------------------------------

def start_transfer(ext: Extension, from_user, to_user, request=None) -> ExtensionTransfer:
    if ext.owner_id != from_user.pk and not from_user.is_orga(ext.event):
        raise ExtensionError(_("You do not own this extension."))
    if from_user == to_user:
        raise ExtensionError(_("Cannot transfer to yourself."))
    if not quota_ok(ext.event, to_user):
        raise ExtensionError(_("The recipient has reached their quota."))
    tr = ExtensionTransfer.objects.create(extension=ext, from_user=from_user, to_user=to_user)
    log(action="transfer", actor=from_user, target=ext, request=request,
        message=f"Transfer offered to {to_user}")
    return tr


@transaction.atomic
def accept_transfer(tr: ExtensionTransfer, user, request=None) -> Extension:
    if not tr.is_open:
        raise ExtensionError(_("This transfer is no longer valid."))
    if tr.to_user_id != user.pk:
        raise ExtensionError(_("This transfer is not addressed to you."))
    ext = tr.extension
    old_owner = ext.owner
    ext.owner = user
    ext.save(update_fields=["owner", "updated_at"])
    tr.accepted_at = timezone.now()
    tr.save(update_fields=["accepted_at"])
    log(action="transfer", actor=user, target=ext, request=request,
        changes={"owner": [str(old_owner), str(user)]}, message="Transfer accepted")
    emit("extension.transferred", _payload(ext), event=ext.event)
    return ext


# --- guest extensions ----------------------------------------------------

def create_guest_extension(event, number: str, actor, expires_at=None, request=None) -> Extension:
    if not event.allow_guest_extensions:
        raise ExtensionError(_("Guest extensions are disabled for this event."))
    ext = register(event, None, number, ExtensionType.DECT, request=request, force_active=True,
                   is_temporary=True, display_name=f"Guest {number}",
                   expires_at=expires_at or timezone.make_aware(
                       timezone.datetime.combine(event.end_date, timezone.datetime.max.time())),
                   in_phonebook=False)
    ext.issue_claim_token()
    ext.state = Extension.State.SUSPENDED  # inactive until claimed
    ext.save(update_fields=["claim_token", "state"])
    log(action="create", actor=actor, target=ext, request=request, message="Guest extension created")
    return ext


@transaction.atomic
def claim_guest_extension(token: str, user, request=None) -> Extension:
    ext = Extension.objects.filter(claim_token=token, is_temporary=True).exclude(
        state__in=[Extension.State.DELETED, Extension.State.EXPIRED]).first()
    if ext is None:
        raise ExtensionError(_("Invalid or expired claim code."))
    if ext.owner_id and ext.owner_id != user.pk:
        raise ExtensionError(_("This extension was already claimed."))
    ext.owner = user
    ext.display_name = user.username[:60]
    ext.state = Extension.State.ACTIVE
    ext.claim_token = ""
    ext.save()
    log(action="update", actor=user, target=ext, request=request, message="Guest extension claimed")
    _on_activated(ext)
    return ext


def _payload(ext: Extension) -> dict:
    return {
        "id": str(ext.pk),
        "event": ext.event.slug,
        "number": ext.number,
        "type": ext.type,
        "state": ext.state,
        "owner": ext.owner.username if ext.owner else None,
        "display_name": ext.display_name,
    }


# --- forwarding ------------------------------------------------------------

FORWARD_MAX_HOPS = 10


def resolve_forward_target(event, number: str) -> Extension | None:
    """The live extension ``number`` in ``event`` (active preferred over requested/suspended)."""
    number = (number or "").strip()
    if not number:
        return None
    live = list(Extension.objects.filter(event=event, number=number, state__in=LIVE_STATES))
    if not live:
        return None
    live.sort(key=lambda e: 0 if e.state == Extension.State.ACTIVE else 1)
    return live[0]


def validate_forward_target(ext: Extension, target: Extension) -> None:
    """Raise ``ExtensionError`` unless ``ext`` may forward to ``target``."""
    if target.pk == ext.pk:
        raise ExtensionError(_("An extension cannot forward to itself."))
    if target.event_id != ext.event_id:
        raise ExtensionError(_("The forwarding target must belong to the same event."))
    if target.state not in LIVE_STATES:
        raise ExtensionError(_("The forwarding target is not an active extension."))
    # walk the chain target -> target.forward_target -> ... and refuse if it comes back to us
    seen = {ext.pk}
    node = target
    for _hop in range(FORWARD_MAX_HOPS):
        if node.pk in seen:
            raise ExtensionError(_("This would create a forwarding loop (%(n)s already forwards back here).")
                                 % {"n": node.number})
        seen.add(node.pk)
        if node.forward_mode == Extension.ForwardMode.OFF or not node.forward_target_id:
            return
        node = Extension.objects.select_related("event").get(pk=node.forward_target_id)
    raise ExtensionError(_("The forwarding chain is too long (max. %(n)d hops).") % {"n": FORWARD_MAX_HOPS})


def set_forwarding(ext: Extension, actor, *, mode: str, target: Extension | None = None,
                   delay: int | None = None, request=None) -> Extension:
    """Set FK-based forwarding. ``mode`` other than ``off`` requires a validated ``target``."""
    if mode not in Extension.ForwardMode.values:
        raise ExtensionError(_("Unknown forwarding mode."))
    if mode != Extension.ForwardMode.OFF:
        if target is None:
            raise ExtensionError(_("Please choose a forwarding target."))
        validate_forward_target(ext, target)
    changes = {}
    if ext.forward_mode != mode:
        changes["forward_mode"] = [ext.forward_mode, mode]
        ext.forward_mode = mode
    new_target_id = target.pk if target is not None else None
    if ext.forward_target_id != new_target_id:
        changes["forward_target"] = [ext.forward_target_number or None, target.number if target else None]
        ext.forward_target = target
    if delay is not None and ext.forward_delay != delay:
        changes["forward_delay"] = [ext.forward_delay, delay]
        ext.forward_delay = delay
    if changes:
        ext.save(update_fields=[*changes, "updated_at"])
        log(action="update", actor=actor, target=ext, request=request, changes=changes)
        emit("extension.updated", _payload(ext), event=ext.event)
        if ext.is_active:
            from apps.extensions.tasks import provision_extension

            provision_extension.delay(str(ext.pk))
    return ext


def clear_forwards_to(target: Extension, actor=None, request=None) -> list[Extension]:
    """Switch off forwarding on every extension that forwards to ``target`` and re-provision them.

    Called when ``target`` is deleted/expired/rejected. Idempotent.
    """
    forwarders = list(Extension.objects.filter(forward_target=target).select_related("event"))
    if not forwarders:
        return []
    from apps.extensions.tasks import provision_extension

    for fwd in forwarders:
        changes = {"forward_target": [target.number, None]}
        if fwd.forward_mode != Extension.ForwardMode.OFF:
            changes["forward_mode"] = [fwd.forward_mode, Extension.ForwardMode.OFF]
        fwd.forward_target = None
        fwd.forward_mode = Extension.ForwardMode.OFF
        fwd.save(update_fields=["forward_target", "forward_mode", "updated_at"])
        log(action="update", actor=actor, target=fwd, request=request, changes=changes,
            message=f"Forwarding cleared: target {target.number} is no longer available")
        if fwd.is_active:
            provision_extension.delay(str(fwd.pk))
    return forwarders


# --- ringback tone -----------------------------------------------------------

def set_ringback_tone(ext: Extension, uploaded_file, actor, request=None) -> Extension:
    """Store a new (already validated) upload and queue the conversion."""
    from apps.extensions.audio import process_ringback_tone

    if ext.ringback_tone:
        ext.ringback_tone.delete(save=False)
    ext.ringback_tone.save(uploaded_file.name, uploaded_file, save=False)
    ext.ringback_tone_status = Extension.RingbackStatus.PENDING
    ext.ringback_tone_error = ""
    ext.save(update_fields=["ringback_tone", "ringback_tone_status", "ringback_tone_error", "updated_at"])
    log(action="update", actor=actor, target=ext, request=request, message="Ringback tone uploaded",
        changes={"ringback_tone": [None, uploaded_file.name]})
    process_ringback_tone.delay(str(ext.pk))
    return ext


def clear_ringback_tone(ext: Extension, actor, request=None) -> Extension:
    if not ext.ringback_tone and ext.ringback_tone_status == Extension.RingbackStatus.NONE:
        return ext
    had_tone = ext.has_ringback_tone
    if ext.ringback_tone:
        ext.ringback_tone.delete(save=False)
    if ext.ringback_tone_processed:
        ext.ringback_tone_processed.delete(save=False)
    ext.ringback_tone = None
    ext.ringback_tone_processed = None
    ext.ringback_tone_status = Extension.RingbackStatus.NONE
    ext.ringback_tone_error = ""
    ext.save(update_fields=["ringback_tone", "ringback_tone_processed", "ringback_tone_status",
                            "ringback_tone_error", "updated_at"])
    log(action="update", actor=actor, target=ext, request=request, message="Ringback tone removed")
    if had_tone and ext.is_active:
        from apps.extensions.tasks import provision_extension

        provision_extension.delay(str(ext.pk))
    return ext
