"""Abstract PBX adapter.

DIAL stores the source of truth (extensions, devices, groups). The adapter
translates that into PBX state and offers call-control primitives used by
callbacks, wake-up calls, emergency broadcast and the test ringback service.

Reference implementation: :mod:`apps.pbx.backends.asterisk` (ARI + AMI +
realtime tables). Others (FreeSWITCH, Kamailio) implement the same class.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.devices.models import Device
    from apps.extensions.models import Extension


@dataclass
class ChannelState:
    """A live channel/call leg as seen by the PBX."""

    id: str
    caller: str
    callee: str
    state: str  # ringing / up / busy / down
    started_at: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class ExtensionStatus:
    number: str
    state: str  # idle / inuse / busy / ringing / unavailable
    registered_devices: int = 0
    active_calls: int = 0


class PBXError(Exception):
    pass


class PBXAdapter(abc.ABC):
    """Interface every PBX backend must implement."""

    name = "abstract"

    # --- provisioning ------------------------------------------------------
    @abc.abstractmethod
    def sync_extension(self, ext: Extension) -> None:
        """Create/update the extension, its device endpoints and routing on the PBX."""

    @abc.abstractmethod
    def remove_extension(self, ext: Extension) -> None:
        """Remove the extension (and dangling endpoints) from the PBX."""

    @abc.abstractmethod
    def sync_device(self, device: Device) -> None:
        """Create/update SIP credentials / endpoint for a device."""

    @abc.abstractmethod
    def remove_device(self, device: Device) -> None:
        pass

    def sync_event(self, event) -> int:
        """Full resync of an event. Returns number of extensions synced."""
        from apps.extensions.models import Extension

        n = 0
        for ext in Extension.objects.filter(event=event).active():
            self.sync_extension(ext)
            n += 1
        return n

    # --- status ------------------------------------------------------------
    @abc.abstractmethod
    def extension_status(self, ext: Extension) -> ExtensionStatus:
        pass

    @abc.abstractmethod
    def active_channels(self, event=None) -> list[ChannelState]:
        pass

    def health(self) -> dict:
        """Return a dict with at least ``{"ok": bool}``."""
        return {"ok": True, "backend": self.name}

    # --- call control ------------------------------------------------------
    @abc.abstractmethod
    def originate(self, *, event, destination: str, caller_id: str, context: str = "dial-services",
                  variables: dict | None = None, timeout: int = 30) -> str:
        """Originate a call to ``destination`` (an extension number) from a service context.

        Returns a channel id. Used by callbacks, wake-up calls and test ringback.
        """

    @abc.abstractmethod
    def hangup(self, channel_id: str) -> None:
        pass

    def broadcast(self, *, event, numbers: list[str], announcement: str, priority: int = 10) -> list[str]:
        """Emergency broadcast: ring all ``numbers`` and play ``announcement`` (path or TTS text)."""
        ids = []
        for n in numbers:
            ids.append(self.originate(event=event, destination=n, caller_id="EMERGENCY",
                                      variables={"DIAL_ANNOUNCEMENT": announcement,
                                                 "DIAL_PRIORITY": str(priority)}))
        return ids

    # --- messaging (MWI etc.) ---------------------------------------------
    def set_mwi(self, ext: Extension, new_messages: int, old_messages: int = 0) -> None:
        """Message-waiting indication. Optional."""

    # --- dialplan export --------------------------------------------------
    def render_dialplan(self, event) -> str:
        """Optional: render a static dialplan snippet for the event (debug/backup)."""
        return ""
