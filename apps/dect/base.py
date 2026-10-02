"""Abstract DECT system adapter (reference implementation: Mitel SIP-DECT OMM via AXI)."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class RFPInfo:
    id: str
    name: str
    mac: str = ""
    ip: str = ""
    connected: bool = False
    synced: bool = False
    active: bool = True
    cluster: str = ""
    location: str = ""
    sync_source: str = ""
    active_calls: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class HandsetInfo:
    ppn: str  # portable part number (OMM internal id)
    ipei: str
    subscribed: bool
    user_id: str = ""
    number: str = ""
    rfp_id: str = ""  # currently serving RFP
    model: str = ""
    battery: int | None = None
    rssi: int | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class SubscriptionResult:
    ppn: str
    user_id: str
    pin: str


class DECTError(Exception):
    pass


class DECTAdapter(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    def health(self) -> dict:
        """``{"ok": bool, "version": str, ...}``"""

    # --- infrastructure ----------------------------------------------------
    @abc.abstractmethod
    def list_rfps(self) -> list[RFPInfo]:
        pass

    @abc.abstractmethod
    def list_handsets(self) -> list[HandsetInfo]:
        pass

    # --- subscription lifecycle -------------------------------------------
    @abc.abstractmethod
    def create_subscription(self, *, ipei: str, number: str, display_name: str,
                            sip_user: str, sip_password: str, pin: str,
                            encryption: bool = False) -> SubscriptionResult:
        """Create PP + user on the OMM and open subscription with ``pin``.

        ``encryption`` requests DECT air-interface encryption for this handset (if the system supports it).
        """

    @abc.abstractmethod
    def update_subscription(self, *, ppn: str, number: str, display_name: str,
                            encryption: bool | None = None, sip_user: str | None = None,
                            sip_password: str | None = None) -> None:
        """Update number/name of an existing subscription; ``encryption=None`` leaves the setting untouched.

        ``sip_user``/``sip_password`` (both given) replace the SIP identity of the handset's user record -
        used when PET adopts a handset that was created on the DECT system without PET (claim pool).
        """

    @abc.abstractmethod
    def delete_subscription(self, ppn: str) -> None:
        pass

    def attach_user(self, *, ppn: str, number: str, display_name: str, sip_user: str, sip_password: str,
                    pin: str) -> str:
        """Create a user record for an already existing PP device (one without a user). Returns the user id.

        Optional: adapters that cannot do this raise ``DECTError``.
        """
        raise DECTError(f"{self.name}: attaching a user to an existing handset is not supported")

    def open_subscription_window(self, minutes: int = 30) -> None:
        """Allow subscription of handsets with a valid PIN for ``minutes``."""

    # --- messaging ---------------------------------------------------------
    def send_message(self, *, ppn: str, text: str, priority: str = "normal") -> bool:
        """Send a text message to a handset. Returns True if accepted."""
        return False

    def set_mwi(self, ppn: str, count: int) -> None:
        pass
