"""GURU3-style handset claiming: dial ``<claim number><code>`` from any subscribed handset to bind it.

Flow
1. A handset gets onto the DECT network before DIAL knows whose it is. Two ways in:
   - **pool**: the orga adds the IPEI on the *DECT handsets* page ("Add pool handset"); DIAL creates the
     subscription with a fresh PIN and a temporary number (``add_pool_handset``), or
   - **adoption**: the OMM auto-creates the handset on subscription (event-wide AC) and
     ``sync_infrastructure`` hands the unknown IPEI to ``adopt_handset`` - DIAL gives it its own SIP
     identity, pushes that to the OMM user record and creates the Asterisk endpoint.
   Either way DIAL ends up with ``Device(unclaimed=True)`` that can dial out.
2. The owner's extension page shows "dial 9004 123456" (``claim_dial_string``).
3. Asterisk answers, POSTs ``dect-claim`` with the caller's PJSIP endpoint and the code.
4. ``claim_handset`` binds the device to the extension, renumbers it on the OMM and returns the number
   for the announcement. Dialling another extension's code later simply moves the handset.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.audit import log as audit_log
from apps.dect import get_dect, provisioning
from apps.dect.base import HandsetInfo
from apps.devices.models import Device, DeviceBinding, generate_pin
from apps.events.webhooks import emit
from apps.extensions.models import Extension, ExtensionType
from apps.extensions.services import get_plan

log = logging.getLogger(__name__)

DECT = "dect"
IPEI_RE = re.compile(r"^\d{13}$")
LIVE_STATES = (Extension.State.REQUESTED, Extension.State.ACTIVE, Extension.State.SUSPENDED)


@dataclass
class ClaimResult:
    ok: bool
    code: str  # claimed | unknown_handset | unknown_code | not_dect | feature_off
    message: str = ""
    device: Device | None = None
    extension: Extension | None = None
    number: str = ""


# --------------------------------------------------------------------------- codes / display

def enabled(plan) -> bool:
    return bool((plan.dect_claim_number or "").strip())


def ensure_claim_code(ext: Extension) -> str:
    """Existing extensions predate the field - mint a code on first use."""
    if not ext.dect_claim_code:
        ext.issue_dect_claim_code()
    return ext.dect_claim_code


def claim_dial_string(plan, ext: Extension) -> str:
    """What the owner dials, or ``""`` when the feature is off / the extension is not a DECT number."""
    if not enabled(plan) or ext.type != ExtensionType.DECT or not ext.is_active:
        return ""
    return f"{plan.dect_claim_number.strip()}{ensure_claim_code(ext)}"


def pool_display_name(plan) -> str:
    """Name shown on an unclaimed handset's display (OMM user name, max 20 chars): a dialling hint."""
    if enabled(plan):
        return f"Dial {plan.dect_claim_number.strip()}+code"[:20]
    return "DIAL pool"


def unclaimed_devices(event):
    return Device.objects.filter(event=event, type=DECT, unclaimed=True)


def temp_number(event, plan) -> str:
    """Temporary OMM number for a pool handset: ``<claim number>NNN``. Not dialable within DIAL; dialling
    it just lands in the claim service with a bogus code."""
    base = (plan.dect_claim_number or "").strip() or "99"
    used = {(d.config or {}).get("temp_number") for d in unclaimed_devices(event).only("config")}
    for n in range(1, 1000):
        cand = f"{base}{n:03d}"
        if cand not in used:
            return cand
    return f"{base}{generate_pin(4)}"


# --------------------------------------------------------------------------- pool handsets

def push_unclaimed(device: Device, *, actor=None) -> Device:
    """(Re)push an unclaimed pool handset to PBX + OMM under its temporary number."""
    plan = get_plan(device.event)
    cfg = dict(device.config or {})
    if not cfg.get("temp_number"):
        cfg["temp_number"] = temp_number(device.event, plan)
        device.config = cfg
    try:
        return provisioning._push(device, number=cfg["temp_number"], display_name=pool_display_name(plan),
                                  actor=actor)
    except Exception as exc:
        provisioning._record_error(device, exc)
        raise


def add_pool_handset(event, ipei: str, *, actor=None, name: str = "") -> Device:
    """Orga path: register ``ipei`` as an unclaimed pool handset and open subscription for it."""
    ipei = "".join(ch for ch in (ipei or "") if ch.isdigit())
    if not IPEI_RE.match(ipei):
        raise ValueError(_("IPEI must be 13 digits."))
    if Device.objects.filter(event=event, ipei=ipei).exists():
        raise ValueError(_("A handset with this IPEI already exists in this event."))
    plan = get_plan(event)
    device = Device(event=event, type=DECT, ipei=ipei, unclaimed=True, name=name[:80],
                    config={"temp_number": temp_number(event, plan), "pool": True})
    device.issue_subscription_pin(save=False)
    device.save()
    audit_log(action="create", actor=actor, target=device, event=event,
              message=f"Pool handset {ipei} added (temp number {device.config['temp_number']})")
    push_unclaimed(device, actor=actor)
    return device


def adopt_handset(event, hs: HandsetInfo, *, actor=None) -> Device | None:
    """Discovery path: a subscribed handset the OMM knows but DIAL does not → unclaimed pool device.

    Gives it DIAL SIP credentials (written to the OMM user record) and an Asterisk endpoint so it can dial
    the claim number. Only active while the plan has a claim number; otherwise unknown handsets stay ignored.
    Returns the device, or ``None`` when nothing was adopted.
    """
    from apps.pbx import get_pbx

    if not hs.subscribed or not IPEI_RE.match(hs.ipei or "") or not hs.ppn:
        return None
    plan = get_plan(event)
    if not enabled(plan):
        return None
    if Device.objects.filter(event=event, ipei=hs.ipei).exists():
        return None
    number = (hs.number or "").strip() if (hs.number or "").strip().isdigit() else ""
    device = Device(event=event, type=DECT, ipei=hs.ipei, omm_ppn=str(hs.ppn), omm_user_id=str(hs.user_id or ""),
                    unclaimed=True, state=Device.State.SUBSCRIBED, handset_model=(hs.model or "")[:60],
                    config={"temp_number": number or temp_number(event, plan), "adopted": True})
    device.ensure_sip_credentials(save=False)
    device.save()
    display = pool_display_name(plan)
    try:
        get_pbx(event).sync_device(device)
        dect = get_dect(event)
        if device.omm_user_id:
            dect.update_subscription(ppn=device.omm_ppn, number=device.config["temp_number"], display_name=display,
                                     sip_user=device.sip_username, sip_password=device.sip_password)
        else:
            device.omm_user_id = str(dect.attach_user(
                ppn=device.omm_ppn, number=device.config["temp_number"], display_name=display,
                sip_user=device.sip_username, sip_password=device.sip_password, pin=generate_pin()) or "")
            device.save(update_fields=["omm_user_id", "updated_at"])
    except Exception as exc:  # noqa: BLE001 - keep the device; the orga sees the error badge
        log.warning("adopted handset %s could not be provisioned: %s", hs.ipei, exc)
        provisioning._record_error(device, exc)
    audit_log(action="create", actor=actor, target=device, event=event,
              message=f"Handset {hs.ipei} adopted from the DECT system into the claim pool",
              changes={"ppn": device.omm_ppn, "temp_number": device.config["temp_number"]})
    emit("device.adopted", {"device": str(device.pk), "ipei": device.ipei, "ppn": device.omm_ppn,
                            "temp_number": device.config["temp_number"]}, event=event)
    return device


# --------------------------------------------------------------------------- claiming

def resolve_caller(event, *idents: str) -> Device | None:
    """Find the calling handset: PJSIP endpoint name (= ``sip_username``), temporary pool number, or the
    number of an extension it is bound to. Tries each identifier in order."""
    from apps.dect.services import _device_for_number

    qs = Device.objects.filter(event=event, type=DECT)
    for ident in idents:
        ident = (ident or "").strip()
        if not ident:
            continue
        device = qs.filter(sip_username=ident).first()
        if device is None:
            device = qs.filter(unclaimed=True, config__temp_number=ident).first()
        if device is None and ident.isdigit():
            device = _device_for_number(event, ident)
        if device is not None:
            return device
    return None


def claim_handset(event, caller: str, code: str, *, callerid: str = "", actor=None) -> ClaimResult:
    """Bind the handset identified by ``caller`` to the active DECT extension whose claim code is ``code``."""
    from apps.extensions.tasks import provision_extension

    plan = get_plan(event)
    if not enabled(plan):
        return ClaimResult(False, "feature_off", _("Handset claiming is not enabled for this event."))
    device = resolve_caller(event, caller, callerid)
    if device is None:
        log.info("dect-claim: unknown caller %r/%r in %s", caller, callerid, event.slug)
        return ClaimResult(False, "unknown_handset", _("Unknown handset."))
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    ext = (Extension.objects.filter(event=event, dect_claim_code=code, state=Extension.State.ACTIVE)
           .select_related("owner").first()) if code else None
    if ext is None:
        return ClaimResult(False, "unknown_code", _("Unknown claim code."), device=device)
    if ext.type != ExtensionType.DECT:
        return ClaimResult(False, "not_dect", _("This extension does not take DECT handsets."), device=device,
                           extension=ext)

    with transaction.atomic():
        others = [b.extension for b in device.bindings.exclude(extension=ext).select_related("extension")]
        device.bindings.exclude(extension=ext).delete()
        binding, created = DeviceBinding.objects.get_or_create(extension=ext, device=device,
                                                               defaults={"priority": 0})
        if not created and not binding.is_active:
            binding.is_active = True
            binding.save(update_fields=["is_active", "updated_at"])
        was_unclaimed, old_owner = device.unclaimed, device.owner
        cfg = dict(device.config or {})
        cfg.pop("temp_number", None)
        cfg["claimed_at"] = timezone.now().isoformat()
        cfg["claimed_via"] = "dial"
        device.unclaimed = False
        device.owner = ext.owner
        device.config = cfg
        device.save()

    message = ""
    try:
        provisioning.provision_device(device, actor=actor)  # renumber on the OMM + caller ID on the PBX
    except Exception as exc:  # noqa: BLE001 - binding is stored; the extension's provisioning retries via outbox
        log.warning("dect-claim: provisioning %s after claim failed: %s", device, exc)
        message = str(exc)
    provision_extension.delay(str(ext.pk))
    for old in others:
        if old.is_active:
            provision_extension.delay(str(old.pk))

    audit_log(action="update", actor=actor or ext.owner, target=ext, event=event,
              message=f"Handset {device.ipei or device.sip_username} claimed by dialling the claim code",
              changes={"device": str(device.pk), "moved_from": [o.number for o in others],
                       "was_unclaimed": was_unclaimed,
                       "previous_owner": str(old_owner) if old_owner and old_owner != ext.owner else None})
    emit("device.claimed", {"device": str(device.pk), "ipei": device.ipei, "extension": ext.number,
                            "owner": ext.owner.username if ext.owner else None,
                            "moved_from": [o.number for o in others]}, event=event)
    return ClaimResult(True, "claimed", message, device=device, extension=ext, number=ext.number)
