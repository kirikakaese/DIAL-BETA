"""AXI client: framing, seq correlation, error handling, RFP/handset parsing - with a fake socket."""
import re
from xml.etree import ElementTree as ET

import pytest

from apps.dect.backends.omm import AXIConnection, AXITransportError, MitelOMM
from apps.dect.base import DECTError


class FakeSocket:
    """Answers each request through ``handlers[tag](elem) -> xml string`` and echoes the seq."""

    def __init__(self, handlers):
        self.handlers = handlers
        self.sent = []
        self.out = b""
        self.closed = False

    def settimeout(self, t):
        pass

    def close(self):
        self.closed = True

    def sendall(self, data):
        assert data.endswith(b"\x00")
        el = ET.fromstring(data[:-1].decode())
        self.sent.append(el)
        resp = self.handlers[el.tag](el)
        if resp is None:
            return
        if isinstance(resp, str):
            resp = [resp]
        for r in resp:
            if "seq=" not in r and "<Event" not in r:
                r = re.sub(r"^<(\w+)", rf'<\1 seq="{el.get("seq")}"', r, count=1)
            self.out += r.encode() + b"\x00"

    def recv(self, n):
        chunk, self.out = self.out[:n], self.out[n:]
        if not chunk and self.closed:
            return b""
        return chunk


OPEN = '<OpenResp ommVersion="SIP-DECT 8.1SP2" axiVersion="1.2" protocolVersion="45"/>'


def make_client(handlers):
    handlers = {"Open": lambda e: OPEN, **handlers}
    sock = FakeSocket(handlers)
    client = MitelOMM({"HOST": "omm.test", "PORT": 12622, "USER": "omm", "PASSWORD": "pw"},
                      sock_factory=lambda h, p, t: sock)
    return client, sock


def test_open_and_health():
    client, sock = make_client({"GetRFPSummary": lambda e: '<GetRFPSummaryResp nRFPs="3" nConnected="2"/>'})
    h = client.health()
    assert h["ok"] is True and h["version"] == "SIP-DECT 8.1SP2" and h["rfps"] == 3 and h["rfps_connected"] == 2
    open_el = sock.sent[0]
    assert open_el.tag == "Open" and open_el.get("login") == "omm" and open_el.get("password") == "pw"
    assert open_el.get("UserDeviceSyncSupport") == "true"


def test_health_never_raises_without_host():
    client = MitelOMM({"HOST": ""})
    h = client.health()
    assert h["ok"] is False and "not configured" in h["error"]


def test_list_rfps_pages_and_parses():
    def get_rfp(e):
        start = int(e.get("startId"))
        assert e.get("withDetails") == "true" and e.get("withState") == "true"
        if start == 0:
            rows = "".join(
                f'<rfp id="{i}" name="RFP-{i}" ethAddr="00:30:42:00:00:{i:02x}" ipAddr="10.0.0.{i}" cluster="1" '
                f'connected="true" syncState="Synced" location="Hall {i}" nActiveCalls="1"/>' for i in range(20))
            return f"<GetRFPResp>{rows}</GetRFPResp>"
        return ('<GetRFPResp><rfp id="20" name="RFP-20" connected="false" syncState="NotConnected" cluster="2"/>'
                '</GetRFPResp>')

    client, sock = make_client({"GetRFP": get_rfp})
    rfps = client.list_rfps()
    assert len(rfps) == 21
    assert rfps[0].connected and rfps[0].synced and rfps[0].cluster == "1" and rfps[0].active_calls == 1
    assert rfps[0].mac == "00:30:42:00:00:00" and rfps[0].location == "Hall 0"
    assert rfps[20].connected is False and rfps[20].synced is False and rfps[20].cluster == "2"
    starts = [e.get("startId") for e in sock.sent if e.tag == "GetRFP"]
    assert starts == ["0", "20"]


def test_list_handsets_joins_users():
    client, _ = make_client({
        "GetPPDev": lambda e: '<GetPPDevResp><pp ppn="7" ipei="0123456789012" subscribed="true" uid="3" rfpId="2"/>'
                              '</GetPPDevResp>',
        "GetPPUser": lambda e: '<GetPPUserResp><user uid="3" name="alice" num="4242" sipAuthId="demo-1"/>'
                               '</GetPPUserResp>',
    })
    hs = client.list_handsets()
    assert len(hs) == 1
    h = hs[0]
    assert h.ppn == "7" and h.ipei == "0123456789012" and h.subscribed and h.number == "4242" and h.rfp_id == "2"
    assert h.extra["name"] == "alice"


def test_create_subscription_builds_device_then_user():
    client, sock = make_client({
        "CreatePPDevice": lambda e: '<CreatePPDeviceResp><pp ppn="12" ipei="0123456789012"/></CreatePPDeviceResp>',
        "CreatePPUser": lambda e: '<CreatePPUserResp><user uid="34"/></CreatePPUserResp>',
    })
    res = client.create_subscription(ipei="0123456789012", number="4242", display_name="alice",
                                     sip_user="demo-abc", sip_password="s3cret", pin="123456")
    assert res.ppn == "12" and res.user_id == "34" and res.pin == "123456"
    dev = next(e for e in sock.sent if e.tag == "CreatePPDevice").find("pp")
    assert dev.get("ipei") == "0123456789012" and dev.get("ac") == "123456" and dev.get("subscribeToPARI") == "true"
    usr = next(e for e in sock.sent if e.tag == "CreatePPUser").find("user")
    assert usr.attrib == {"name": "alice", "num": "4242", "sipAuthId": "demo-abc", "sipPw": "s3cret",
                          "pin": "123456", "ppn": "12", "relType": "Fixed"}


def test_create_subscription_rolls_back_device_on_user_error():
    client, sock = make_client({
        "CreatePPDevice": lambda e: '<CreatePPDeviceResp><pp ppn="12"/></CreatePPDeviceResp>',
        "CreatePPUser": lambda e: '<CreatePPUserResp errCode="EInval" info="num already in use"/>',
        "DeletePPDevice": lambda e: "<DeletePPDeviceResp/>",
    })
    with pytest.raises(DECTError, match="EInval num already in use"):
        client.create_subscription(ipei="0123456789012", number="4242", display_name="a", sip_user="u",
                                   sip_password="p", pin="1")
    assert [e.tag for e in sock.sent][-1] == "DeletePPDevice"
    assert sock.sent[-1].get("ppn") == "12"


def test_update_and_delete_subscription():
    client, sock = make_client({
        "GetPPDev": lambda e: f'<GetPPDevResp><pp ppn="{e.get("startPPN")}" uid="9"/></GetPPDevResp>',
        "SetPPUser": lambda e: "<SetPPUserResp/>",
        "DeletePPUser": lambda e: "<DeletePPUserResp/>",
        "DeletePPDevice": lambda e: "<DeletePPDeviceResp/>",
        "SetDECTSubscriptionMode": lambda e: "<SetDECTSubscriptionModeResp/>",
    })
    client.update_subscription(ppn="5", number="4243", display_name="bob")
    upd = next(e for e in sock.sent if e.tag == "SetPPUser").find("user")
    assert upd.attrib == {"uid": "9", "name": "bob", "num": "4243"}
    client.delete_subscription("5")
    tags = [e.tag for e in sock.sent]
    assert tags[-2:] == ["DeletePPUser", "DeletePPDevice"]
    client.open_subscription_window(15)
    assert sock.sent[-1].attrib["mode"] == "Configured" and sock.sent[-1].attrib["timeout"] == "15"


def test_seq_correlation_skips_unsolicited_events():
    client, sock = make_client({
        "GetRFPSummary": lambda e: ['<EventRFPState id="1" connected="false"/>',
                                    '<GetRFPSummaryResp nRFPs="1" nConnected="0"/>'],
    })
    assert client.health()["rfps"] == 1
    seqs = [e.get("seq") for e in sock.sent]
    assert seqs == ["1", "2"]  # Open, GetRFPSummary


def test_reconnects_after_transport_error():
    calls = {"n": 0}

    def flaky(e):
        calls["n"] += 1
        if calls["n"] == 1:
            sock.closed = True  # EOF on first attempt
            return None
        return '<GetRFPSummaryResp nRFPs="2"/>'

    client, sock = make_client({"GetRFPSummary": flaky})
    resp = client._call(ET.Element("GetRFPSummary"))
    assert resp.get("nRFPs") == "2"
    assert [e.tag for e in sock.sent] == ["Open", "GetRFPSummary", "Open", "GetRFPSummary"]


def test_transport_error_is_dect_error():
    assert issubclass(AXITransportError, DECTError)
    conn = AXIConnection("h", 1, "u", "p", sock_factory=lambda *a: (_ for _ in ()).throw(OSError("refused")))
    with pytest.raises(AXITransportError, match="cannot connect"):
        conn.connect()


def test_send_message_returns_false_on_error():
    client, _ = make_client({"SendMessage": lambda e: '<SendMessageResp errCode="ELicense"/>'})
    assert client.send_message(ppn="1", text="hi") is False
    client2, sock2 = make_client({"SendMessage": lambda e: "<SendMessageResp/>"})
    assert client2.send_message(ppn="1", text="hi", priority="high") is True
    assert sock2.sent[-1].get("priority") == "Urgent"
