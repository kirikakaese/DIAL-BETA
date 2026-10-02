"""Extensible endpoint abstraction.

Each endpoint type describes how a device of that class is onboarded and how
the PBX should dial it. Register new types with ``@register``.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .models import Device, DeviceType


@dataclass
class EndpointType:
    key: str
    label: str
    description: str
    onboarding: str  # 'ipei_pin', 'sip_credentials', 'webrtc', 'gsm_sim', 'ata'
    user_fields: list[str] = field(default_factory=list)  # fields shown to the user when adding a device
    supports_provisioning: bool = False
    dial_string: Callable[[Device], str] | None = None
    enabled: bool = True

    def dial(self, device: Device) -> str:
        if self.dial_string:
            return self.dial_string(device)
        return f"PJSIP/{device.sip_username}"

    def enabled_for(self, event) -> bool:
        """Whether this device class may be used in ``event``.

        GSM is a per-event decision (an on-site cell network must be attached to the PBX); every
        other class follows the global ``enabled`` flag.
        """
        if self.key == DeviceType.GSM:
            return bool(event is not None and getattr(event, "has_gsm", False))
        return self.enabled


REGISTRY: dict[str, EndpointType] = {}


def register(et: EndpointType) -> EndpointType:
    REGISTRY[et.key] = et
    return et


def get(key: str) -> EndpointType:
    return REGISTRY[key]


def all_types(enabled_only=True):
    return [t for t in REGISTRY.values() if t.enabled or not enabled_only]


def all_types_for(event):
    """Device classes usable in ``event`` (see ``EndpointType.enabled_for``)."""
    return [t for t in REGISTRY.values() if t.enabled_for(event)]


def gsm_dial_string(device: Device) -> str:
    trunk = getattr(device.event, "gsm_trunk", "") or "gsm-gateway"
    return f"PJSIP/{device.msisdn}@{trunk}"


register(EndpointType(
    key=DeviceType.DECT, label="DECT handset",
    description="Any GAP-compatible DECT handset. Subscribe with the IPEI printed under the battery "
                "and a temporary PIN.",
    onboarding="ipei_pin", user_fields=["ipei", "handset_model", "name"],
    dial_string=lambda d: f"PJSIP/{d.sip_username}",
))
register(EndpointType(
    key=DeviceType.SIP, label="SIP endpoint",
    description="Softphone or hardphone. PET generates credentials; scan a QR code or use autoprovisioning.",
    onboarding="sip_credentials", user_fields=["name", "sip_transport", "mac_address"],
    supports_provisioning=True,
))
register(EndpointType(
    key=DeviceType.WEBRTC, label="WebRTC browser phone",
    description="Call directly from the browser over secure WebSockets.",
    onboarding="webrtc", user_fields=["name"],
    dial_string=lambda d: f"PJSIP/{d.sip_username}",
    enabled=False,  # scaffolded; enable when the PBX exposes WSS
))
register(EndpointType(
    key=DeviceType.GSM, label="GSM handset (local cell network)",
    description="Phones on an on-site GSM network (e.g. Osmocom). Routed via the GSM gateway trunk.",
    onboarding="gsm_sim", user_fields=["imsi", "msisdn", "name"],
    dial_string=gsm_dial_string,
    enabled=False,  # per event: see ``EndpointType.enabled_for`` / ``Event.has_gsm``
))
register(EndpointType(
    key=DeviceType.ANALOG, label="Analog phone via ATA",
    description="Rotary or push-button phones on an analog terminal adapter.",
    onboarding="ata", user_fields=["name", "mac_address"],
    supports_provisioning=True,
    enabled=False,
))
