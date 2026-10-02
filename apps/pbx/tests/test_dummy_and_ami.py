"""Dummy backend records everything; AMI protocol parsing."""
import socket

import pytest

from apps.pbx.ami import AMIClient, AMIError
from apps.pbx.backends.dummy import DummyPBX
from apps.pbx.base import ChannelState

from .conftest import make_extension

pytestmark = pytest.mark.django_db


def test_dummy_records(event, user):
    pbx = DummyPBX()
    ext = make_extension(event, "4242", owner=user)
    pbx.sync_extension(ext)
    assert pbx.extensions == {str(ext.pk): "4242"}
    cid = pbx.originate(event=event, destination="4242", caller_id="x", variables={"a": "1"})
    assert pbx.originated[0]["destination"] == "4242" and pbx.originated[0]["variables"] == {"a": "1"}
    assert [c.id for c in pbx.active_channels(event)] == [cid]
    pbx.hangup(cid)
    assert pbx.hungup == [cid] and pbx.active_channels() == []
    pbx.set_mwi(ext, 3, 1)
    assert pbx.mwi == [("4242", 3, 1)]
    pbx.channels = [{"id": "z", "caller": "1", "callee": "2", "state": "up"}]
    assert isinstance(pbx.active_channels()[0], ChannelState)
    assert pbx.sync_event(event) == 1 and pbx.synced_events == ["demo"]
    pbx.remove_extension(ext)
    assert pbx.removed_extensions == [str(ext.pk)] and pbx.extensions == {}
    assert "[dial-demo]" in pbx.render_dialplan(event)
    assert pbx.health()["ok"] is True
    pbx.reset()
    assert pbx.snapshot()["originated"] == []


# --------------------------------------------------------------------------- AMI

class FakeSocket:
    """In-memory AMI peer: answers each action with the scripted lines for it."""

    def __init__(self, script):
        self.script = script
        self.rx = b"Asterisk Call Manager/7.0.3\r\n"
        self.received = []
        self._buf = b""

    def sendall(self, data):
        self._buf += data
        while b"\r\n\r\n" in self._buf:
            pkt, _, self._buf = self._buf.partition(b"\r\n\r\n")
            headers = {}
            for line in pkt.decode().split("\r\n"):
                if ": " in line:
                    k, v = line.split(": ", 1)
                    headers.setdefault(k, []).append(v)
            flat = {k: (v[0] if len(v) == 1 else v) for k, v in headers.items()}
            self.received.append(flat)
            lines = self.script.get(flat.get("Action"), ["Response: Success", "ActionID: {aid}"])
            out = [line.replace("{aid}", str(flat.get("ActionID", ""))) for line in lines]
            self.rx += ("\r\n".join(out) + "\r\n\r\n").encode()

    def recv(self, n):
        chunk, self.rx = self.rx[:n], self.rx[n:]
        return chunk

    def close(self):
        pass


@pytest.fixture
def fake_ami(monkeypatch):
    holder = {}

    def factory(script):
        sock = FakeSocket(script)
        holder["sock"] = sock
        monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: sock)
        return sock

    return factory


def test_ami_login_and_originate(fake_ami):
    sock = fake_ami({
        "Login": ["Response: Success", "ActionID: {aid}", "Message: Authentication accepted"],
        "Originate": ["Event: Newchannel", "Channel: noise", "", "Response: Success", "ActionID: {aid}",
                      "Message: Originate successfully queued"],
        "Logoff": ["Response: Goodbye", "ActionID: {aid}"],
    })
    with AMIClient("127.0.0.1", 5038, "dial", "pw") as ami:
        cid = ami.originate(channel="Local/4242@dial-demo", context="dial-services", exten="announce",
                            caller_id="x", variables={"A": "1", "B": "2"})
    assert cid.startswith("dial-")
    orig = [r for r in sock.received if r.get("Action") == "Originate"][0]
    assert orig["Channel"] == "Local/4242@dial-demo" and orig["Async"] == "true"
    assert orig["Variable"] == ["A=1", "B=2"]
    assert sock.received[0]["Action"] == "Login" and sock.received[0]["Secret"] == "pw"
    assert sock.received[-1]["Action"] == "Logoff"


def test_ami_login_failure(fake_ami):
    fake_ami({"Login": ["Response: Error", "ActionID: {aid}", "Message: Authentication failed"]})
    with pytest.raises(AMIError, match="Authentication failed"):
        AMIClient("127.0.0.1", 5038, "dial", "bad").connect()


def test_ami_device_state_list(fake_ami):
    fake_ami({
        "Login": ["Response: Success", "ActionID: {aid}"],
        "DeviceStateList": ["Response: Success", "ActionID: {aid}", "EventList: start", "",
                            "Event: DeviceStateChange", "Device: PJSIP/a", "State: NOT_INUSE", "",
                            "Event: DeviceStateChange", "Device: PJSIP/b", "State: INUSE", "",
                            "Event: DeviceStateListComplete", "EventList: Complete", "ListItems: 2"],
    })
    with AMIClient("127.0.0.1", 5038, "dial", "pw") as ami:
        assert ami.device_state_list() == {"PJSIP/a": "NOT_INUSE", "PJSIP/b": "INUSE"}
        assert ami.reload() is True


def test_ami_connection_error(monkeypatch):
    def refuse(*a, **kw):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(socket, "create_connection", refuse)
    with pytest.raises(AMIError, match="refused"):
        AMIClient("127.0.0.1", 5038, "dial", "pw", timeout=1).connect()
