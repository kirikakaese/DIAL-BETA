"""Policy evaluation for trunk number blocks (``4700``-``4799`` routed to one SIP trunk).

A block is described by its *base* (the extension number, ending in ``digits`` zeros) and the number of
trailing wildcard digits. The whole block must be registrable: the base and the last number are evaluated
against the plan, every reserved plan number (service, emergency, digit-only feature codes) that falls
inside the block denies it, and ranges whose prefix lies inside the block are sampled so a blocked or
restricted sub-range cannot hide in the middle of a block. Trunks always need approval (unless an orga
registers them).
"""
from __future__ import annotations

from django.utils.translation import gettext as _

from apps.extensions.models import TRUNK_BLOCK_DIGITS, ExtensionType

from .models import NumberPlan, PolicyResult


def block_prefix(base: str, digits: int) -> str:
    return base[:-digits] if digits else base


def block_last(base: str, digits: int) -> str:
    return block_prefix(base, digits) + "9" * digits


def validate_block(base: str, digits: int) -> str:
    """Formal check of base/digits; returns an error message or ``""``."""
    base = (base or "").strip()
    if not base.isdigit():
        return _("Extension must consist of digits only.")
    if digits not in TRUNK_BLOCK_DIGITS:
        return _("A trunk block has 10, 100 or 1000 numbers.")
    if len(base) <= digits:
        return _("The block base is too short for a block of this size.")
    if base[-digits:] != "0" * digits:
        return _("The block base must end in %(n)d zeros (e.g. %(example)s).") % {
            "n": digits, "example": base[:-digits] + "0" * digits}
    return ""


def evaluate_block(plan: NumberPlan, base: str, digits: int, user=None,
                   extension_type: str = ExtensionType.TRUNK) -> PolicyResult:
    """May ``user`` register the block ``base``/``digits`` as a trunk? Never checks *taken* numbers."""
    base = (base or "").strip()
    err = validate_block(base, digits)
    if err:
        return PolicyResult.deny(err, code="format")
    prefix = block_prefix(base, digits)
    length = len(base)

    first = plan.evaluate(base, user=user, extension_type=extension_type)
    if not first.allowed:
        return first
    last = plan.evaluate(block_last(base, digits), user=user, extension_type=extension_type)
    if not last.allowed:
        return last

    emergency = {str(n).strip() for n in (plan.emergency_numbers or [])}
    for reserved in sorted(set(plan.reserved_numbers())):
        if len(reserved) == length and reserved.startswith(prefix):
            code = "emergency" if reserved in emergency else "service"
            return PolicyResult.deny(
                _("The block contains the reserved number %(n)s.") % {"n": reserved}, code=code)

    # ranges that start inside the block (prefix longer than the block prefix): sample their first number
    for rng in plan.active_ranges():
        p = (rng.prefix or "").strip()
        if not p or len(p) <= len(prefix) or len(p) > length or not p.startswith(prefix):
            continue
        sample = p + "0" * (length - len(p))
        res = plan.evaluate(sample, user=user, extension_type=extension_type)
        if not res.allowed:
            return res

    return PolicyResult.allow(requires_approval=True, range=first.range)
