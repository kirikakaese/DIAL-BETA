"""Device services: DECT vendor registry + handset history, GSM registration, autoprovisioning lookups.

Kept free of request/response handling so the portal views, the ``/prov/`` endpoints and the PBX
hooks share the same logic.
"""
from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.audit import log as audit_log

from .models import DECTManufacturer, Device, DeviceBinding, DeviceType

log = logging.getLogger(__name__)


class DeviceServiceError(Exception):
    pass


class GSMRegisterError(DeviceServiceError):
    pass


# --------------------------------------------------------------------------- DECT vendor registry

def normalize_ipei(ipei: str | None) -> str:
    return "".join(ch for ch in (ipei or "") if ch.isdigit())


def emc_of(ipei: str | None) -> str:
    """First 5 digits of the IPEI (Equipment Manufacturer Code), ``""`` when too short."""
    digits = normalize_ipei(ipei)
    return digits[:5] if len(digits) >= 5 else ""


def vendor_for_emc(emc: str) -> DECTManufacturer | None:
    if not emc:
        return None
    return DECTManufacturer.objects.filter(emc=emc).first()


def vendor_for_ipei(ipei: str | None) -> DECTManufacturer | None:
    return vendor_for_emc(emc_of(ipei))


def suggest_vendor(emc: str, name: str, *, models_hint: str = "", actor=None, event=None) -> DECTManufacturer:
    """Crowdsourcing: record a user-supplied vendor for an unknown EMC. Never overwrites existing rows."""
    emc = normalize_ipei(emc)[:5]
    name = (name or "").strip()
    if len(emc) != 5:
        raise DeviceServiceError(_("The EMC must be 5 digits."))
    if not name:
        raise DeviceServiceError(_("Please enter a vendor name."))
    obj, created = DECTManufacturer.objects.get_or_create(
        emc=emc, defaults={"name": name[:80], "models_hint": (models_hint or "")[:500],
                           "source": DECTManufacturer.Source.USER},
    )
    if created:
        audit_log(action="create", actor=actor, target=obj, event=event,
                  message=f"DECT vendor {name!r} suggested for EMC {emc}")
    return obj


def upsert_vendor(emc: str, name: str, *, models_hint: str = "", actor=None, event=None) -> DECTManufacturer:
    """Orga curation: create or rename a manufacturer entry."""
    emc = normalize_ipei(emc)[:5]
    name = (name or "").strip()
    if len(emc) != 5:
        raise DeviceServiceError(_("The EMC must be 5 digits."))
    if not name:
        raise DeviceServiceError(_("Please enter a vendor name."))
    obj, created = DECTManufacturer.objects.get_or_create(
        emc=emc, defaults={"name": name[:80], "models_hint": (models_hint or "")[:500],
                           "source": DECTManufacturer.Source.USER},
    )
    changes = {}
    if not created:
        if obj.name != name[:80]:
            changes["name"] = [obj.name, name[:80]]
            obj.name = name[:80]
        if models_hint and obj.models_hint != models_hint[:500]:
            changes["models_hint"] = [obj.models_hint, models_hint[:500]]
            obj.models_hint = models_hint[:500]
        if changes:
            obj.save(update_fields=[*changes.keys(), "updated_at"])
    audit_log(action="create" if created else "update", actor=actor, target=obj, event=event,
              message=f"DECT vendor {'added' if created else 'renamed'}: {emc} {obj.name}", changes=changes or None)
    return obj


def manufacturer_stats(event) -> list[dict]:
    """``[{"manufacturer": DECTManufacturer|None, "emc": str, "count": int}]`` for handsets in ``event``.

    Unknown EMCs are listed too (``manufacturer=None``) so orga can see what needs a vendor entry.
    """
    ipeis = Device.objects.filter(event=event, type=DeviceType.DECT).exclude(ipei="").values_list("ipei", flat=True)
    counts: dict[str, int] = {}
    for ipei in ipeis:
        emc = emc_of(ipei)
        if emc:
            counts[emc] = counts.get(emc, 0) + 1
    known = {m.emc: m for m in DECTManufacturer.objects.all()}
    rows = [{"manufacturer": known.get(emc), "emc": emc, "count": n} for emc, n in counts.items()]
    for emc, m in known.items():
        if emc not in counts:
            rows.append({"manufacturer": m, "emc": emc, "count": 0})
    rows.sort(key=lambda r: (-r["count"], r["emc"]))
    return rows


# --------------------------------------------------------------------------- handset history

@dataclass
class HandsetUse:
    device: Device
    event: object
    numbers: list[str] = field(default_factory=list)


@dataclass
class HandsetHistory:
    ipei: str
    vendor: DECTManufacturer | None
    model: str
    name: str
    uses: list[HandsetUse] = field(default_factory=list)

    @property
    def latest(self) -> Device:
        return self.uses[0].device

    def device_in(self, event) -> Device | None:
        for u in self.uses:
            if u.event.pk == event.pk:
                return u.device
        return None


def handset_history(user) -> list[HandsetHistory]:
    """The user's DECT handsets across all events, grouped by IPEI (newest event first).

    ``Device.ipei`` is unique per *event*, so the same physical handset is one ``Device`` row per
    event. The vendor is looked up once per EMC.
    """
    devices = (Device.objects.filter(owner=user, type=DeviceType.DECT).exclude(ipei="")
               .select_related("event").order_by("-event__start_date", "created_at"))
    bindings = (DeviceBinding.objects.filter(device__in=devices).select_related("extension")
                .order_by("priority"))
    numbers: dict = {}
    for b in bindings:
        numbers.setdefault(b.device_id, []).append(b.extension.number)
    vendors = {m.emc: m for m in DECTManufacturer.objects.filter(
        emc__in={emc_of(d.ipei) for d in devices} - {""})}
    grouped: dict[str, HandsetHistory] = {}
    for d in devices:
        h = grouped.get(d.ipei)
        if h is None:
            h = grouped[d.ipei] = HandsetHistory(ipei=d.ipei, vendor=vendors.get(emc_of(d.ipei)),
                                                 model=d.handset_model, name=d.name)
        else:
            h.model = h.model or d.handset_model
            h.name = h.name or d.name
        h.uses.append(HandsetUse(device=d, event=d.event, numbers=numbers.get(d.pk, [])))
    return list(grouped.values())


def reuse_handset(old: Device, event, user, *, request=None) -> tuple[Device, bool]:
    """Copy a handset from a previous event into ``event`` for ``user``.

    Returns ``(device, created)``; when the IPEI already exists in ``event`` the existing device is
    returned with ``created=False``.
    """
    if old.type != DeviceType.DECT or not old.ipei:
        raise DeviceServiceError(_("Only DECT handsets with an IPEI can be reused."))
    existing = Device.objects.filter(event=event, ipei=old.ipei).first()
    if existing is not None:
        return existing, False
    device = Device.objects.create(
        event=event, owner=user, type=DeviceType.DECT, state=Device.State.NEW,
        ipei=old.ipei, handset_model=old.handset_model, name=old.name, uak=old.uak,
    )
    audit_log(action="create", actor=user, target=device, event=event, request=request,
              message=f"Handset {old.ipei} reused from event {old.event.slug}")
    return device, True


# --------------------------------------------------------------------------- GSM

def gsm_devices(event):
    return (Device.objects.filter(event=event, type=DeviceType.GSM)
            .select_related("owner").prefetch_related("bindings__extension").order_by("created_at"))


def create_gsm_device(event, *, name="", msisdn="", imsi="", extension=None, owner=None, actor=None,
                      request=None) -> Device:
    """Orga helper: add a GSM handset (optionally bound to ``extension``) and issue its register token."""
    if extension is not None and extension.event_id != event.pk:
        raise DeviceServiceError(_("Extension does not belong to this event."))
    owner = owner or (extension.owner if extension is not None else None)
    device = Device.objects.create(event=event, owner=owner, type=DeviceType.GSM, name=name,
                                   msisdn=(msisdn or "").strip(), imsi=(imsi or "").strip())
    device.issue_gsm_register_token()
    if extension is not None:
        DeviceBinding.objects.get_or_create(extension=extension, device=device,
                                            defaults={"priority": extension.bindings.count()})
    audit_log(action="create", actor=actor, target=device, event=event, request=request,
              message=f"GSM device added{f' to {extension.number}' if extension is not None else ''}")
    return device


@transaction.atomic
def gsm_register(event, token: str, imsi: str, msisdn: str = "", *, actor=None) -> Device:
    """Hook for the GSM core: a subscriber dialled/texted ``token`` -> bind IMSI/MSISDN to that device.

    The token is single-use and cleared on success.
    """
    token = (token or "").strip()
    imsi = "".join(ch for ch in (imsi or "") if ch.isdigit())
    if not token:
        raise GSMRegisterError("missing token")
    if not imsi:
        raise GSMRegisterError("missing imsi")
    device = (Device.objects.select_for_update()
              .filter(event=event, type=DeviceType.GSM, gsm_register_token=token)
              .exclude(state=Device.State.DISABLED).first())
    if device is None:
        raise GSMRegisterError("unknown or expired token")
    changes = {"imsi": [device.imsi, imsi]}
    device.imsi = imsi
    if msisdn:
        changes["msisdn"] = [device.msisdn, msisdn]
        device.msisdn = msisdn.strip()
    device.gsm_register_token = ""
    device.gsm_registered_at = timezone.now()
    device.state = Device.State.SUBSCRIBED
    device.save(update_fields=["imsi", "msisdn", "gsm_register_token", "gsm_registered_at", "state", "updated_at"])
    audit_log(action="provision", actor=actor, target=device, event=event, message="GSM SIM registered",
              changes=changes)
    _reprovision_bound_extensions(device)
    return device


def _reprovision_bound_extensions(device: Device):
    try:
        from apps.extensions.tasks import provision_extension
    except ImportError:  # pragma: no cover - extensions app is always present
        return
    for b in device.bindings.filter(is_active=True).select_related("extension"):
        if b.extension.is_active:
            transaction.on_commit(lambda pk=str(b.extension.pk): provision_extension.delay(pk))


# --------------------------------------------------------------------------- autoprovisioning

def normalize_mac(mac: str | None) -> str:
    return "".join(ch for ch in (mac or "") if ch.isalnum()).lower()


def mac_variants(mac: str) -> list[str]:
    """Spellings a MAC may have been stored with (``mac_address`` is free text)."""
    plain = normalize_mac(mac)
    if len(plain) != 12:
        return [plain] if plain else []
    pairs = [plain[i:i + 2] for i in range(0, 12, 2)]
    return [plain, ":".join(pairs), "-".join(pairs), plain.upper(), ":".join(pairs).upper(), "-".join(pairs).upper()]


def _provisionable():
    return (Device.objects.exclude(state=Device.State.DISABLED).exclude(provisioning_profile=None)
            .select_related("event", "provisioning_profile"))


def device_for_provisioning_token(token: str) -> Device | None:
    if not token or len(token) < 16:
        return None
    return _provisionable().filter(provisioning_token=token).first()


def device_for_directory_token(token: str) -> Device | None:
    """Any enabled device behind ``/prov/<token>/phonebook.xml`` (a vendor profile is optional)."""
    if not token or len(token) < 16:
        return None
    return (Device.objects.exclude(state=Device.State.DISABLED).filter(provisioning_token=token)
            .select_related("event", "provisioning_profile").first())


def device_for_softphone_token(token: str) -> Device | None:
    """SIP device behind a ``/prov/<token>/<client>.xml`` softphone URL (no vendor profile required)."""
    if not token or len(token) < 16:
        return None
    return (Device.objects.exclude(state=Device.State.DISABLED).exclude(sip_username="").exclude(sip_password="")
            .filter(type=DeviceType.SIP, provisioning_token=token).select_related("event").first())


def device_for_mac(vendor: str, mac: str) -> Device | None:
    variants = mac_variants(mac)
    if not variants:
        return None
    return _provisionable().filter(mac_address__in=variants, provisioning_profile__vendor=vendor).first()


def filename_matches(device: Device, filename: str) -> bool:
    expected = device.provisioning_filename
    return bool(expected) and filename.lower() == expected.lower()


def check_provisioning_auth(device: Device, *, token: str = "", username: str = "", password: str = "") -> bool:
    """Either the provisioning token or the device's own SIP credentials."""
    if token and device.provisioning_token:
        return hmac.compare_digest(token, device.provisioning_token)
    if username and password and device.sip_username and device.sip_password:
        return (hmac.compare_digest(username, device.sip_username)
                and hmac.compare_digest(password, device.sip_password))
    return False


def render_provisioning(device: Device) -> tuple[str, str]:
    """``(body, content_type)`` for a device with a profile; records ``config["last_provisioned_at"]``."""
    profile = device.provisioning_profile
    body = profile.render(device)
    cfg = dict(device.config or {})
    cfg["last_provisioned_at"] = timezone.now().isoformat(timespec="seconds")
    Device.objects.filter(pk=device.pk).update(config=cfg)
    return body, profile.content_type or "text/plain"
