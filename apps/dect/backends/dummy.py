"""In-memory DECT adapter for tests, development and demos.

Simulates a small venue: two sync clusters with several RFPs, a couple of handsets parked
on them, and (optionally) a flapping base station. Enable random flapping with
``DIAL_DECT_DUMMY_FLAP = True`` to exercise alerting in a dev environment.

Deterministic by default: ``list_rfps()`` returns the same set on every call.
"""
import random

from django.conf import settings

from apps.dect.base import DECTAdapter, DECTError, HandsetInfo, RFPInfo, SubscriptionResult

# id, name, cluster, location, mac suffix
DUMMY_RFPS = [
    ("1", "RFP-Main-Stage", "1", "Main stage, truss left", "01"),
    ("2", "RFP-Foodcourt", "1", "Food court, mast", "02"),
    ("3", "RFP-Infodesk", "1", "Info desk", "03"),
    ("4", "RFP-Camp-North", "2", "Camping north, tower", "04"),
    ("5", "RFP-Camp-South", "2", "Camping south, tower", "05"),
    ("6", "RFP-Workshop", "2", "Workshop tent", "06"),
]


class DummyDECT(DECTAdapter):
    name = "dummy"

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.subs: dict[str, dict] = {}       # ppn -> {"ipei", "number", "pin", "display_name", "rfp_id", ...}
        self.messages: list[dict] = []        # send_message() log
        self.calls: list[tuple[str, dict]] = []  # (method, kwargs) log of subscription create/update/delete
        self.mwi: dict[str, int] = {}         # ppn -> count
        self.subscription_window_minutes = 0
        self.rfp_state: dict[str, dict] = {
            rid: {"connected": True, "synced": True, "active_calls": 0} for rid, *_ in DUMMY_RFPS
        }
        # one deliberately unsynced RFP so the "degraded" path is visible in demos
        self.rfp_state["6"]["synced"] = False
        self._rng = random.Random(42)
        self._ppn_seq = 0

    # -- test/demo helpers ------------------------------------------------------
    def set_rfp(self, rfp_id: str, *, connected=None, synced=None, active_calls=None):
        st = self.rfp_state[str(rfp_id)]
        if connected is not None:
            st["connected"] = connected
        if synced is not None:
            st["synced"] = synced
        if active_calls is not None:
            st["active_calls"] = active_calls

    def place_handset(self, ppn: str, rfp_id: str):
        self.subs[str(ppn)]["rfp_id"] = str(rfp_id)

    def _maybe_flap(self):
        if not getattr(settings, "DIAL_DECT_DUMMY_FLAP", False):
            return
        for rid in ("5", "6"):  # only the camp RFPs flap - keeps the demo dashboard readable
            if self._rng.random() < 0.3:
                self.rfp_state[rid]["connected"] = not self.rfp_state[rid]["connected"]
        for st in self.rfp_state.values():
            st["active_calls"] = self._rng.randint(0, 4) if st["connected"] else 0

    # -- adapter -----------------------------------------------------------------
    def health(self):
        return {"ok": True, "backend": "dummy", "version": "sim-1.0", "error": None}

    def list_rfps(self):
        self._maybe_flap()
        out = []
        for rid, name, cluster, location, mac in DUMMY_RFPS:
            st = self.rfp_state[rid]
            out.append(RFPInfo(
                id=rid, name=name, mac=f"00:30:42:0d:00:{mac}", ip=f"10.99.0.{int(rid) + 10}",
                connected=st["connected"], synced=st["connected"] and st["synced"], cluster=cluster,
                location=location, sync_source="" if rid in ("1", "4") else ("1" if cluster == "1" else "4"),
                active_calls=st["active_calls"],
            ))
        return out

    def list_handsets(self):
        return [
            HandsetInfo(ppn=p, ipei=s["ipei"], subscribed=s.get("subscribed", True), user_id=s.get("user_id", p),
                        number=s["number"], rfp_id=s.get("rfp_id", "1"), model="Mitel 612d",
                        battery=s.get("battery", 80), rssi=s.get("rssi", -60))
            for p, s in self.subs.items()
        ]

    def create_subscription(self, *, ipei, number, display_name, sip_user, sip_password, pin, encryption=False):
        self.calls.append(("create_subscription", {"ipei": ipei, "number": number, "display_name": display_name,
                                                   "pin": pin, "encryption": bool(encryption)}))
        for p, s in self.subs.items():
            if s["ipei"] == ipei:
                raise DECTError(f"IPEI {ipei} already subscribed as ppn {p}")
        self._ppn_seq += 1
        ppn = str(self._ppn_seq)
        # spread handsets over RFPs so coverage views have something to show
        rfp_id = DUMMY_RFPS[(self._ppn_seq - 1) % len(DUMMY_RFPS)][0]
        self.subs[ppn] = {"ipei": ipei, "number": number, "pin": pin, "display_name": display_name,
                          "sip_user": sip_user, "sip_password": sip_password, "rfp_id": rfp_id,
                          "user_id": ppn, "subscribed": True, "encryption": bool(encryption)}
        return SubscriptionResult(ppn=ppn, user_id=ppn, pin=pin)

    def update_subscription(self, *, ppn, number, display_name, encryption=None, sip_user=None, sip_password=None):
        self.calls.append(("update_subscription", {"ppn": ppn, "number": number, "display_name": display_name,
                                                   "encryption": encryption}))
        if ppn not in self.subs:
            raise DECTError(f"unknown ppn {ppn}")
        self.subs[ppn]["number"] = number
        self.subs[ppn]["display_name"] = display_name
        if encryption is not None:
            self.subs[ppn]["encryption"] = bool(encryption)
        if sip_user and sip_password:
            self.subs[ppn]["sip_user"], self.subs[ppn]["sip_password"] = sip_user, sip_password

    def attach_user(self, *, ppn, number, display_name, sip_user, sip_password, pin):
        if ppn not in self.subs:
            raise DECTError(f"unknown ppn {ppn}")
        self.subs[ppn].update(number=number, display_name=display_name, sip_user=sip_user,
                              sip_password=sip_password, pin=pin, user_id=ppn)
        return ppn

    # -- test/demo helper: a handset that subscribed without DIAL knowing it (OMM auto-create) ------------
    def add_foreign_handset(self, ipei: str, *, number: str = "", rfp_id: str = "1", with_user: bool = True) -> str:
        self._ppn_seq += 1
        ppn = str(self._ppn_seq)
        self.subs[ppn] = {"ipei": ipei, "number": number, "pin": "", "display_name": "", "sip_user": "",
                          "sip_password": "", "rfp_id": str(rfp_id), "user_id": ppn if with_user else "",
                          "subscribed": True, "encryption": False}
        return ppn

    def delete_subscription(self, ppn):
        self.calls.append(("delete_subscription", {"ppn": ppn}))
        self.subs.pop(ppn, None)
        self.mwi.pop(ppn, None)

    def open_subscription_window(self, minutes=30):
        self.subscription_window_minutes = minutes

    def send_message(self, *, ppn, text, priority="normal"):
        if ppn not in self.subs:
            return False
        self.messages.append({"ppn": ppn, "text": text, "priority": priority})
        return True

    def set_mwi(self, ppn, count):
        self.mwi[ppn] = count
