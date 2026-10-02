"""ARI client + call-control methods with ``requests`` mocked (no network)."""
import json
from unittest import mock

import pytest
import requests

from apps.pbx.ari import ARIClient, ARIError
from apps.pbx.base import PBXError

from .conftest import bind, make_device, make_extension

pytestmark = pytest.mark.django_db


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.content = (json.dumps(payload).encode() if payload is not None else text.encode())
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.reason = "OK" if status < 400 else "Error"

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def ari_request():
    with mock.patch("requests.Session.request") as m:
        yield m


def test_originate_posts_channel(pbx, event, ari_request):
    ari_request.return_value = FakeResponse(200, {"id": "chan-1", "name": "Local/4242@pet-demo-0000;1"})
    cid = pbx.originate(event=event, destination="4242", caller_id="Callback",
                        variables={"PET_SERVICE": "callback", "PET_CALLBACK_TARGET": "4300"}, timeout=25)
    assert cid == "chan-1"
    method, url = ari_request.call_args.args
    kw = ari_request.call_args.kwargs
    assert method == "POST" and url == "http://asterisk:8088/ari/channels"
    assert kw["params"]["endpoint"] == "Local/4242@pet-demo"
    assert kw["params"]["context"] == "pet-services"
    assert kw["params"]["extension"] == "callback"
    assert kw["params"]["callerId"] == "Callback" and kw["params"]["timeout"] == 25
    v = kw["json"]["variables"]
    assert v["PET_EVENT"] == "demo" and v["PET_CALLBACK_TARGET"] == "4300" and v["PET_DESTINATION"] == "4242"
    assert "PET_SERVICE" not in v


def test_broadcast_uses_announce_service(pbx, event, ari_request):
    ari_request.return_value = FakeResponse(200, {"id": "x"})
    ids = pbx.broadcast(event=event, numbers=["4242", "4243"], announcement="pet/evacuate")
    assert ids == ["x", "x"] and ari_request.call_count == 2
    assert ari_request.call_args.kwargs["params"]["extension"] == "announce"
    assert ari_request.call_args.kwargs["json"]["variables"]["PET_ANNOUNCEMENT"] == "pet/evacuate"


def test_hangup_deletes_channel(pbx, ari_request):
    ari_request.return_value = FakeResponse(204)
    pbx.hangup("chan/1")
    method, url = ari_request.call_args.args
    assert method == "DELETE" and url == "http://asterisk:8088/ari/channels/chan%2F1"


def test_network_error_raises_pbxerror(pbx, event, ari_request):
    ari_request.side_effect = requests.ConnectionError("boom")
    with pytest.raises(PBXError) as exc:
        pbx.hangup("chan-1")
    assert "ConnectionError" in str(exc.value)
    with pytest.raises(PBXError):
        pbx.originate(event=event, destination="4242", caller_id="x")


def test_http_error_raises_with_status(pbx, ari_request):
    ari_request.return_value = FakeResponse(404, {"message": "Channel not found"})
    with pytest.raises(ARIError) as exc:
        pbx.hangup("nope")
    assert exc.value.status == 404 and "Channel not found" in str(exc.value)


def test_health_ok_and_down(pbx, ari_request):
    ari_request.return_value = FakeResponse(200, {"system": {"version": "20.6.0", "entity_id": "aa"},
                                                 "status": {"startup_time": "t0", "last_reload_time": "t1"}})
    h = pbx.health()
    assert h == {"ok": True, "backend": "asterisk", "version": "20.6.0", "entity_id": "aa",
                 "startup_time": "t0", "last_reload_time": "t1"}
    ari_request.side_effect = requests.Timeout("slow")
    h = pbx.health()
    assert h["ok"] is False and "Timeout" in h["error"]


def test_active_channels_filtered_by_event(pbx, event, ari_request):
    ari_request.return_value = FakeResponse(200, [
        {"id": "1", "name": "PJSIP/demo-aaaa-0001", "state": "Up", "accountcode": "demo",
         "caller": {"number": "4242", "name": "Alice"}, "connected": {"number": "4243"},
         "dialplan": {"context": "pet-demo", "exten": "4243"}, "creationtime": "2026-09-13T10:00:00"},
        {"id": "2", "name": "PJSIP/x", "state": "Ringing", "accountcode": "other",
         "caller": {"number": "1"}, "connected": {"number": ""}, "dialplan": {"context": "pet-other", "exten": "2"}},
    ])
    chans = pbx.active_channels(event)
    assert len(chans) == 1
    c = chans[0]
    assert c.id == "1" and c.caller == "4242" and c.callee == "4243" and c.state == "up"
    assert c.extra["context"] == "pet-demo"
    assert len(pbx.active_channels()) == 2
    assert pbx.active_channels()[1].state == "ringing"


def test_extension_status(pbx, event, user, ari_request):
    ext = make_extension(event, "4242", owner=user)
    bind(ext, make_device(event, "demo-aaaa"))
    bind(ext, make_device(event, "demo-bbbb"))

    def fake(method, url, **kw):
        if url.endswith("/endpoints/PJSIP/demo-aaaa"):
            return FakeResponse(200, {"state": "online", "channel_ids": ["c1"]})
        if url.endswith("/endpoints/PJSIP/demo-bbbb"):
            return FakeResponse(200, {"state": "offline", "channel_ids": []})
        if url.endswith("/deviceStates/PJSIP%2Fdemo-aaaa"):
            return FakeResponse(200, {"name": "PJSIP/demo-aaaa", "state": "INUSE"})
        if url.endswith("/deviceStates/PJSIP%2Fdemo-bbbb"):
            return FakeResponse(200, {"name": "PJSIP/demo-bbbb", "state": "UNAVAILABLE"})
        return FakeResponse(404, {"message": "nope"})

    ari_request.side_effect = fake
    st = pbx.extension_status(ext)
    assert st.number == "4242" and st.state == "inuse"
    assert st.registered_devices == 1 and st.active_calls == 1


def test_extension_status_without_devices(pbx, event, user, ari_request):
    ext = make_extension(event, "4242", owner=user)
    assert pbx.extension_status(ext).state == "unavailable"
    ari_request.assert_not_called()


def test_set_mwi_via_mailboxes(pbx, event, user, ari_request):
    ext = make_extension(event, "4242", owner=user)
    ari_request.return_value = FakeResponse(204)
    pbx.set_mwi(ext, 2, 1)
    method, url = ari_request.call_args.args
    assert method == "PUT" and url == "http://asterisk:8088/ari/mailboxes/4242%40pet-demo"
    assert ari_request.call_args.kwargs["params"] == {"newMessages": 2, "oldMessages": 1}
    # module not loaded -> warning, no exception
    ari_request.return_value = FakeResponse(404, {"message": "Resource not found"})
    pbx.set_mwi(ext, 1)
    ari_request.return_value = FakeResponse(500, {"message": "kaboom"})
    with pytest.raises(PBXError):
        pbx.set_mwi(ext, 1)


def test_originate_falls_back_to_ami(pbx, event, ari_request):
    ari_request.side_effect = requests.ConnectionError("down")
    pbx.use_ami = True
    fake_ami = mock.MagicMock()
    fake_ami.__enter__.return_value = fake_ami
    fake_ami.originate.return_value = "pet-abc"
    with mock.patch.object(pbx, "_ami", return_value=fake_ami):
        assert pbx.originate(event=event, destination="4242", caller_id="x") == "pet-abc"
    fake_ami.originate.assert_called_once()
    assert fake_ami.originate.call_args.kwargs["channel"] == "Local/4242@pet-demo"


def test_ari_client_invalid_json():
    client = ARIClient("http://a/ari", "u", "p")
    with mock.patch("requests.Session.request", return_value=FakeResponse(200, None, text="<html>")):
        with pytest.raises(ARIError):
            client.info()
