"""DECT provisioning glue between extensions/devices and the DECT system (OMM).

Contract used by other apps:
- ``provision_dect_for_extension(ext)``: push all DECT devices bound to ``ext`` to the OMM
- ``deprovision_dect_for_extension(ext)``: remove subscriptions for ext's DECT devices
- ``provision_device(device)``: push a single device (e.g. after a new PIN)

Per-device failures are recorded in ``device.config["provision_error"]`` and re-raised (as one
``DECTError`` summarising all failures) after every device has been attempted, so the caller
(``apps.extensions.tasks.provision_extension``) can store the error on the extension.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.core.audit import log as audit_log
from apps.dect import get_dect
from apps.dect.base import DECTError
from apps.devices.models import Device, DeviceBinding
from apps.events.webhooks import emit

log = logging.getLogger(__name__)

DECT = "dect"


def _dect_devices(ext):
    return Device.objects.filter(bindings__extension=ext, bindings__is_active=True, type=DECT).distinct()


def _primary_number(device: Device, prefer=None):
    """The number the handset is provisioned with: its lowest-priority active binding."""
    if prefer is not None:
        return prefer
    b = (DeviceBinding.objects.filter(device=device, is_active=True, extension__state="active")
         .select_related("extension").order_by("priority").first())
    return b.extension if b else None


def _record_error(device: Device, exc: Exception):
    cfg = dict(device.config or {})
    cfg["provision_error"] = str(exc)[:500]
    cfg["provision_error_at"] = timezone.now().isoformat()
    device.config = cfg
    device.save(update_fields=["config", "updated_at"])


def _clear_error(device: Device):
    if device.config and "provision_error" in device.config:
        cfg = dict(device.config)
        cfg.pop("provision_error", None)
        cfg.pop("provision_error_at", None)
        device.config = cfg


def _push_device(device: Device, ext, *, actor=None) -> Device:
    """Ensure SIP credentials + PBX endpoint, then create/update the OMM subscription."""
    return _push(device, number=ext.number, display_name=ext.caller_id_name,
                 encryption=bool(getattr(ext, "dect_encryption", False)), actor=actor)


def _push(device: Device, *, number: str, display_name: str, encryption: bool = False, actor=None) -> Device:
    from apps.pbx import get_pbx

    device.ensure_sip_credentials()
    get_pbx(device.event).sync_device(device)

    if not device.ipei:
        raise DECTError("device has no IPEI")
    if not device.subscription_pin and not device.omm_ppn:
        device.issue_subscription_pin(save=False)

    dect = get_dect(device.event)
    created = False
    cfg = dict(device.config or {})
    pin_changed = bool(device.omm_ppn) and cfg.get("provisioned_pin") not in (None, device.subscription_pin)
    if pin_changed and device.state != Device.State.SUBSCRIBED:
        # A fresh PIN is only useful if the OMM knows it: the AC lives on the PP device record,
        # so recreate the record instead of patching it (SetPPDevice ac= is not portable across releases).
        try:
            dect.delete_subscription(device.omm_ppn)
        except DECTError as exc:
            log.warning("could not delete stale subscription %s: %s", device.omm_ppn, exc)
        device.omm_ppn = device.omm_user_id = ""
    if not device.omm_ppn:
        res = dect.create_subscription(
            ipei=device.ipei, number=number, display_name=display_name,
            sip_user=device.sip_username, sip_password=device.sip_password, pin=device.subscription_pin,
            encryption=encryption,
        )
        device.omm_ppn, device.omm_user_id = res.ppn, res.user_id
        created = True
        if device.state != Device.State.SUBSCRIBED:
            device.state = Device.State.PENDING
        try:
            dect.open_subscription_window(minutes=30)
        except DECTError as exc:  # not fatal - orga can open it on the OMM manually
            log.warning("could not open subscription window: %s", exc)
    else:
        dect.update_subscription(ppn=device.omm_ppn, number=number, display_name=display_name,
                                 encryption=encryption)
        if device.state == Device.State.NEW:
            device.state = Device.State.PENDING
    _clear_error(device)
    cfg = dict(device.config or {})
    cfg["provisioned_pin"] = device.subscription_pin
    device.config = cfg
    device.save()

    audit_log(action="provision", actor=actor, target=device, event=device.event,
              message=f"DECT {'subscription created' if created else 'subscription updated'} for {number}",
              changes={"ppn": device.omm_ppn, "number": number, "created": created})
    emit("device.provisioned", {
        "device": str(device.pk), "ipei": device.ipei, "ppn": device.omm_ppn, "extension": number,
        "created": created, "state": device.state,
    }, event=device.event)
    return device


def provision_dect_for_extension(ext):
    """Push every active DECT device bound to ``ext`` to the OMM. Returns the list of devices pushed.

    A handset carries one identity on the OMM: its primary (lowest-priority) active binding. When ``ext`` is
    only a secondary binding of a device, the primary extension's number/name is (re)pushed instead, so editing
    a secondary extension never renumbers the handset.
    """
    errors, done = [], []
    for device in _dect_devices(ext).select_related("event"):
        try:
            done.append(_push_device(device, _primary_number(device) or ext))
        except Exception as exc:  # noqa: BLE001 - keep going, report all at the end
            log.exception("DECT provisioning failed for %s", device)
            _record_error(device, exc)
            errors.append(f"{device}: {exc}")
    if errors:
        raise DECTError("; ".join(errors))
    return done


def deprovision_dect_for_extension(ext):
    """Delete OMM subscriptions of DECT devices whose *only* binding is ``ext``.

    Devices also bound to another extension are re-pointed at that extension instead.
    """
    errors, removed = [], []
    dect = get_dect(ext.event)
    for device in _dect_devices(ext).select_related("event"):
        try:
            other = (DeviceBinding.objects.filter(device=device, is_active=True)
                     .exclude(extension=ext).exclude(extension__state__in=("deleted", "rejected", "expired"))
                     .select_related("extension").order_by("priority").first())
            if other is not None and device.omm_ppn:
                dect.update_subscription(ppn=device.omm_ppn, number=other.extension.number,
                                         display_name=other.extension.caller_id_name,
                                         encryption=bool(other.extension.dect_encryption))
                continue
            if device.omm_ppn:
                dect.delete_subscription(device.omm_ppn)
            device.omm_ppn = ""
            device.omm_user_id = ""
            if device.state in (Device.State.PENDING, Device.State.SUBSCRIBED):
                device.state = Device.State.NEW
            device.save(update_fields=["omm_ppn", "omm_user_id", "state", "updated_at"])
            removed.append(device)
            audit_log(action="deprovision", target=device, event=device.event,
                      message=f"DECT subscription removed (extension {ext.number})")
            emit("device.deprovisioned", {"device": str(device.pk), "ipei": device.ipei, "extension": ext.number},
                 event=device.event)
        except Exception as exc:  # noqa: BLE001
            log.exception("DECT deprovisioning failed for %s", device)
            _record_error(device, exc)
            errors.append(f"{device}: {exc}")
    if errors:
        raise DECTError("; ".join(errors))
    return removed


def provision_device(device, *, actor=None):
    """Push a single DECT device (used when a new PIN is issued or a binding changes)."""
    if device.type != DECT:
        return None
    ext = _primary_number(device)
    if ext is None:
        raise DECTError("device is not bound to an active extension")
    try:
        return _push_device(device, ext, actor=actor)
    except Exception as exc:
        _record_error(device, exc)
        raise
