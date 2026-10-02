"""Mitel SIP-DECT OMM (OpenMobility Manager) adapter speaking AXI.

AXI ("OM Application XML Interface") is a request/response protocol: each message is a
single XML element, terminated by a NUL byte (``\\x00``), sent over a TLS socket (default port
12622; 12621 is the plain-text variant that newer OMMs no longer accept). The client opens the
session with ``<Open login=".." password=".." UserDeviceSyncSupport="true"/>``; the OMM
answers ``<OpenResp .../>``. Every request may carry a ``seq`` attribute that the OMM echoes
in the response, which we use to correlate replies and skip unsolicited event messages
(``<EventRFPState .../>``, ``<EventDECTSubscriptionMode .../>``, ...). Errors are reported as an
``errCode`` attribute (``ENoEnt``, ``EFailed``, ``EInval``, ``EAuth``...) plus an optional
``info`` attribute on the ``*Resp`` element.

Attribute names below were derived from the AXI reference of SIP-DECT 7.x/8.x and verified
against a 9.x OMM where noted. Where OMM releases differ, the affected XML is isolated in
small ``_build_*`` / ``_parse_*`` methods so an operator can subclass ``MitelOMM`` and set
``PET_DECT_BACKEND`` to the subclass. Only the Python standard library is used.

Messages used:

============================  =====================================================================
``GetRFPSummary``             ``<GetRFPSummaryResp nRFPs= nConnected= .../>``      (7.x+)
``GetRFP``                    ``<rfp id ethAddr ipAddr name location cluster connected syncState
                              syncSource? nActiveCalls?/>`` - paged via ``maxRecords``/``startId``
``GetPPDev`` / ``GetPPUser``  device / user records ``<pp ppn ipei subscribed uid .../>`` and
                              ``<user uid name num sipAuthId ppn .../>`` (paged via ``startPPN``/
                              ``startUid``). 7.x still accepted ``GetPP``; we send GetPPDev/GetPPUser.
``CreatePPDevice``            ``<pp ipei ac subscribeToPARI relType/>`` → ``<pp ppn=.../>``
``CreatePPUser``              ``<user name num sipAuthId sipPw pin ppn relType/>`` → ``<user uid/>``
``SetPPUser`` / ``SetPPDevice``  partial update (only supplied attributes change)
``DeletePPUser uid`` / ``DeletePPDevice ppn``
``SetDECTSubscriptionMode``   ``mode="Off|Configured|Wildcard" timeout=<minutes>`` (8.x+; 7.x had no
                              timeout attribute and it is ignored there)
``SendMessage``               ``<SendMessage ppn|uid text priority=Normal|Urgent|Emergency/>``
                              (needs the OMM messaging license; returns errCode otherwise)
============================  =====================================================================
"""
from __future__ import annotations

import itertools
import logging
import socket
import ssl
import threading
import time
from xml.etree import ElementTree as ET

from django.conf import settings

from apps.dect.base import DECTAdapter, DECTError, HandsetInfo, RFPInfo, SubscriptionResult

log = logging.getLogger(__name__)

NUL = b"\x00"
OK_CODES = {"", "EOk", "OK"}


class AXITransportError(DECTError):
    """Socket-level failure (connect/timeout/EOF/garbage) - safe to reconnect and retry."""


def _b(v: bool) -> str:
    return "true" if v else "false"


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("true", "1", "yes", "on")


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class AXIConnection:
    """One authenticated AXI session over a NUL-delimited XML stream.

    ``sock_factory(host, port, timeout)`` must return a connected socket-like object with
    ``sendall``/``recv``/``close``/``settimeout``; tests inject a fake here.
    """

    def __init__(self, host, port, user, password, *, verify_tls=False, timeout=10.0, sock_factory=None):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.verify_tls, self.timeout = verify_tls, timeout
        self._sock_factory = sock_factory or self._tls_socket
        self.sock = None
        self.buffer = b""
        self.open_resp: ET.Element | None = None
        self.seq = itertools.count(1)
        self.lock = threading.RLock()

    # -- transport ----------------------------------------------------------
    def _tls_socket(self, host, port, timeout):
        raw = socket.create_connection((host, port), timeout=timeout)
        ctx = ssl.create_default_context()
        if not self.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx.wrap_socket(raw, server_hostname=host)

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def connect(self):
        self.close()
        if not self.host:
            raise DECTError("OMM host not configured (settings.OMM['HOST'])")
        try:
            self.sock = self._sock_factory(self.host, self.port, self.timeout)
            self.sock.settimeout(self.timeout)
        except OSError as exc:
            self.sock = None
            raise AXITransportError(f"cannot connect to OMM {self.host}:{self.port}: {exc}") from exc
        self.buffer = b""
        open_el = ET.Element("Open", login=self.user, password=self.password, UserDeviceSyncSupport="true")
        self.open_resp = self.request(open_el, expect="OpenResp")

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.buffer = b""

    # -- framing ------------------------------------------------------------
    def send(self, element: ET.Element):
        data = ET.tostring(element, encoding="unicode").encode("utf-8") + NUL
        try:
            self.sock.sendall(data)
        except OSError as exc:
            raise AXITransportError(f"OMM send failed: {exc}") from exc

    def read_message(self) -> ET.Element:
        deadline = time.monotonic() + self.timeout
        while NUL not in self.buffer:
            if time.monotonic() > deadline:
                raise AXITransportError("OMM response timeout")
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError as exc:
                raise AXITransportError("OMM response timeout") from exc
            except OSError as exc:
                raise AXITransportError(f"OMM read failed: {exc}") from exc
            if not chunk:
                raise AXITransportError("OMM closed the connection")
            self.buffer += chunk
        raw, _, self.buffer = self.buffer.partition(NUL)
        text = raw.decode("utf-8", errors="replace").strip()
        if text.startswith("<?xml"):
            text = text.split("?>", 1)[1]
        try:
            return ET.fromstring(text)
        except ET.ParseError as exc:
            raise AXITransportError(f"invalid XML from OMM: {exc}: {text[:200]!r}") from exc

    def request(self, element: ET.Element, *, expect: str | None = None) -> ET.Element:
        """Send ``element`` and return the correlated response; raise DECTError on ``errCode``."""
        with self.lock:
            seq = str(next(self.seq))
            element.set("seq", seq)
            expect = expect or element.tag + "Resp"
            self.send(element)
            # skip unsolicited events until the answer to *our* request arrives
            for _ in range(200):
                resp = self.read_message()
                if resp.get("seq") == seq or (resp.get("seq") is None and resp.tag == expect):
                    break
                log.debug("AXI: ignoring unsolicited %s", resp.tag)
            else:
                raise AXITransportError("no correlated OMM response received")
        code = resp.get("errCode", "")
        if code not in OK_CODES:
            raise DECTError(f"OMM {element.tag} failed: {code} {resp.get('info', '')}".strip())
        return resp


class MitelOMM(DECTAdapter):
    """DECTAdapter for Mitel SIP-DECT OMM via AXI. See module docstring for protocol notes."""

    name = "mitel-omm"
    page_size = 20
    retries = 2

    def __init__(self, config: dict | None = None, *, sock_factory=None):
        cfg = dict(getattr(settings, "OMM", {}) or {})
        cfg.update(config or {})
        self.config = cfg
        self.conn = AXIConnection(
            cfg.get("HOST", ""), int(cfg.get("PORT") or 12622), cfg.get("USER", "omm"), cfg.get("PASSWORD", ""),
            verify_tls=bool(cfg.get("VERIFY_TLS", False)),
            timeout=float(getattr(settings, "PET_OMM_TIMEOUT", 10)),
            sock_factory=sock_factory,
        )

    # -- plumbing -----------------------------------------------------------
    def _call(self, element: ET.Element, *, expect: str | None = None) -> ET.Element:
        """Request with lazy connect and one reconnect on transport failure."""
        last = None
        for attempt in range(self.retries + 1):
            try:
                if not self.conn.connected:
                    self.conn.connect()
                return self.conn.request(element, expect=expect)
            except AXITransportError as exc:
                last = exc
                if attempt >= self.retries:
                    raise
                log.warning("AXI transport error (%s), reconnecting (attempt %d)", exc, attempt + 1)
                self.conn.close()
                time.sleep(0.2 * (attempt + 1))
        raise last  # pragma: no cover

    def _paged(self, tag: str, child: str, start_attr: str, id_attr: str, **attrs) -> list[ET.Element]:
        """Iterate a ``maxRecords``/``start*`` paged listing until the OMM runs dry."""
        out, start = [], 0
        while True:
            el = ET.Element(tag, maxRecords=str(self.page_size), **{start_attr: str(start)}, **attrs)
            try:
                resp = self._call(el)
            except DECTError as exc:
                if "ENoEnt" in str(exc) and out:  # past the last record
                    break
                raise
            rows = list(resp.iter(child))
            out.extend(rows)
            if len(rows) < self.page_size:
                break
            ids = [_int(r.get(id_attr)) for r in rows if _int(r.get(id_attr)) is not None]
            if not ids:
                break
            start = max(ids) + 1
        return out

    # -- health -------------------------------------------------------------
    def health(self) -> dict:
        try:
            if not self.conn.connected:
                self.conn.connect()
            summary = self._call(ET.Element("GetRFPSummary"))
            o = self.conn.open_resp
            return {
                "ok": True, "backend": self.name,
                "version": (o.get("ommVersion") or o.get("version") or "") if o is not None else "",
                "axi_version": o.get("axiVersion", "") if o is not None else "",
                "rfps": _int(summary.get("nRFPs"), 0), "rfps_connected": _int(summary.get("nConnected"), 0),
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - health must never raise
            self.conn.close()
            return {"ok": False, "backend": self.name, "version": "", "error": str(exc)}

    # -- infrastructure -----------------------------------------------------
    def list_rfps(self) -> list[RFPInfo]:
        rows = self._paged("GetRFP", "rfp", "startId", "id", withDetails="true", withState="true")
        return [self._parse_rfp(r) for r in rows]

    def _parse_rfp(self, el: ET.Element) -> RFPInfo:
        """``syncState`` is one of Synced/Unsynced/Searching/NotConnected (8.x); 7.x used ``synced``."""
        sync_state = el.get("syncState", "")
        synced = sync_state.lower() == "synced" if sync_state else _truthy(el.get("synced"))
        return RFPInfo(
            id=el.get("id", ""), name=el.get("name") or f"RFP {el.get('id', '')}",
            mac=el.get("ethAddr", ""), ip=el.get("ipAddr", ""),
            connected=_truthy(el.get("connected")), synced=synced,
            active=_truthy(el.get("dectOn", "true")), cluster=el.get("cluster", ""),
            location=el.get("location", ""), sync_source=el.get("syncSource", "") or el.get("syncRfp", ""),
            active_calls=_int(el.get("nActiveCalls") or el.get("activeCalls"), 0),
            extra={k: v for k, v in el.attrib.items() if k in ("swVersion", "hwType", "rfpMode", "pagingArea",
                                                                  "siteId", "syncState")},
        )

    def list_handsets(self) -> list[HandsetInfo]:
        devs = self._paged("GetPPDev", "pp", "startPPN", "ppn", withDetails="true", withState="true")
        users = {}
        try:
            for u in self._paged("GetPPUser", "user", "startUid", "uid"):
                users[u.get("uid", "")] = u
        except DECTError as exc:  # user records are optional for monitoring purposes
            log.warning("GetPPUser failed: %s", exc)
        return [self._parse_handset(d, users.get(d.get("uid", ""))) for d in devs]

    def _parse_handset(self, pp: ET.Element, user: ET.Element | None) -> HandsetInfo:
        """State attribute names for the serving RFP / battery differ between releases; try several."""
        rfp_id = pp.get("rfpId") or pp.get("locRfpId") or pp.get("location") or ""
        return HandsetInfo(
            ppn=pp.get("ppn", ""), ipei=pp.get("ipei", ""), subscribed=_truthy(pp.get("subscribed")),
            user_id=pp.get("uid", ""), number=user.get("num", "") if user is not None else "",
            rfp_id=rfp_id, model=pp.get("hwType") or pp.get("ppProfileCapability", "") or "",
            battery=_int(pp.get("battery") or pp.get("batteryLevel")),
            rssi=_int(pp.get("rssi") or pp.get("rxLevel")),
            extra={"relType": pp.get("relType", ""), "name": user.get("name", "") if user is not None else ""},
        )

    # -- subscriptions ------------------------------------------------------
    def _build_pp_device(self, ipei: str, pin: str, encryption: bool = False) -> ET.Element:
        """``ac`` is the authentication code the handset user types; ``subscribeToPARI`` limits the
        subscription to this OMM's PARI (recommended in multi-OMM venues). ``encrypt`` requests DECT
        air-interface encryption for this PP (attribute name from the AXI reference; NOT yet validated on
        hardware - subclass and override if your OMM release rejects it with EInval)."""
        el = ET.Element("CreatePPDevice")
        attrs = {"ipei": ipei, "ac": pin, "subscribeToPARI": "true", "relType": "Fixed"}
        if encryption:
            attrs["encrypt"] = "1"
        ET.SubElement(el, "pp", **attrs)
        return el

    def _build_pp_user(self, *, ppn: str, number: str, display_name: str, sip_user: str, sip_password: str,
                       pin: str) -> ET.Element:
        el = ET.Element("CreatePPUser")
        ET.SubElement(el, "user", name=display_name[:20], num=number, sipAuthId=sip_user, sipPw=sip_password,
                      pin=pin, ppn=ppn, relType="Fixed")
        return el

    def create_subscription(self, *, ipei, number, display_name, sip_user, sip_password, pin,
                            encryption=False) -> SubscriptionResult:
        dev = self._call(self._build_pp_device(ipei, pin, encryption=encryption))
        pp = dev.find("pp")
        ppn = (pp.get("ppn") if pp is not None else None) or dev.get("ppn") or ""
        if not ppn:
            raise DECTError("CreatePPDevice returned no ppn")
        try:
            usr = self._call(self._build_pp_user(ppn=ppn, number=number, display_name=display_name,
                                                 sip_user=sip_user, sip_password=sip_password, pin=pin))
        except DECTError:
            # roll back the orphaned device so a retry can recreate it cleanly
            try:
                self._call(ET.Element("DeletePPDevice", ppn=ppn))
            except DECTError:
                log.warning("could not roll back PP device %s", ppn)
            raise
        user = usr.find("user")
        uid = (user.get("uid") if user is not None else None) or usr.get("uid") or ""
        return SubscriptionResult(ppn=ppn, user_id=uid, pin=pin)

    def _user_for_ppn(self, ppn: str) -> str:
        devs = self._call(ET.Element("GetPPDev", maxRecords="1", startPPN=str(ppn)))
        pp = devs.find("pp")
        if pp is None or pp.get("ppn") != str(ppn):
            raise DECTError(f"PP device {ppn} not found on OMM")
        return pp.get("uid", "")

    def update_subscription(self, *, ppn, number, display_name, encryption=None, sip_user=None,
                            sip_password=None) -> None:
        uid = self._user_for_ppn(ppn)
        if not uid:
            raise DECTError(f"PP device {ppn} has no user record")
        el = ET.Element("SetPPUser")
        attrs = {"uid": uid, "name": display_name[:20], "num": number}
        if sip_user and sip_password:
            attrs.update(sipAuthId=sip_user, sipPw=sip_password)
        ET.SubElement(el, "user", **attrs)
        self._call(el)
        if encryption is not None:
            self._set_pp_encryption(ppn, encryption)

    def attach_user(self, *, ppn, number, display_name, sip_user, sip_password, pin) -> str:
        usr = self._call(self._build_pp_user(ppn=str(ppn), number=number, display_name=display_name,
                                             sip_user=sip_user, sip_password=sip_password, pin=pin))
        user = usr.find("user")
        return (user.get("uid") if user is not None else None) or usr.get("uid") or ""

    def _set_pp_encryption(self, ppn, encryption: bool) -> None:
        """``<SetPPDevice><pp ppn=.. encrypt="1|0"/></SetPPDevice>`` - unvalidated on hardware, so a
        rejection is logged rather than failing the whole provisioning run."""
        el = ET.Element("SetPPDevice")
        ET.SubElement(el, "pp", ppn=str(ppn), encrypt="1" if encryption else "0")
        try:
            self._call(el)
        except DECTError as exc:
            log.warning("OMM rejected encrypt=%s for PP %s: %s", int(encryption), ppn, exc)

    def delete_subscription(self, ppn) -> None:
        try:
            uid = self._user_for_ppn(ppn)
        except DECTError as exc:
            if "not found" in str(exc) or "ENoEnt" in str(exc):
                return  # already gone
            raise
        if uid:
            try:
                self._call(ET.Element("DeletePPUser", uid=uid))
            except DECTError as exc:
                if "ENoEnt" not in str(exc):
                    raise
        try:
            self._call(ET.Element("DeletePPDevice", ppn=str(ppn)))
        except DECTError as exc:
            if "ENoEnt" not in str(exc):
                raise

    def open_subscription_window(self, minutes: int = 30) -> None:
        self._call(ET.Element("SetDECTSubscriptionMode", mode="Configured", timeout=str(int(minutes))))

    # -- messaging ----------------------------------------------------------
    def send_message(self, *, ppn, text, priority="normal") -> bool:
        """OMM messaging (``SendMessage``) requires the messaging licence; ``priority`` maps to
        Normal/Urgent/Emergency (8.x names). Returns False instead of raising when unavailable."""
        prio = {"low": "Normal", "normal": "Normal", "high": "Urgent", "urgent": "Urgent",
                "emergency": "Emergency"}.get(priority.lower(), "Normal")
        try:
            self._call(ET.Element("SendMessage", ppn=str(ppn), text=text[:160], priority=prio))
            return True
        except DECTError as exc:
            log.warning("SendMessage to ppn %s failed: %s", ppn, exc)
            return False

    def set_mwi(self, ppn, count) -> None:
        """No-op: on SIP-DECT the message-waiting lamp is driven by SIP ``NOTIFY`` (message-summary)
        from the PBX to the handset's SIP registration, not by an AXI call. AXI only lets us set the
        ``voiceboxNum`` the handset dials on long-press of the mailbox key, which is configured once
        at user creation time. Keep the PBX's MWI subscription pointed at the device's SIP user."""
        return None
