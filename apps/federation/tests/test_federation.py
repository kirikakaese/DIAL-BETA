from unittest import mock

import pytest
from django.urls import reverse

from apps.federation import services
from apps.federation.models import FederationDirectoryEntry, FederationPeer

pytestmark = pytest.mark.django_db


@pytest.fixture
def peer(event, orga):
    return services.register_peer(event, orga, name="Camp 2", remote_prefix="8", sip_host="pbx.camp2.example",
                                  state="active", auth_user="demo", auth_password="pw")


def test_route(event, peer):
    assert services.route(event, "81234") == f"PJSIP/1234@fed-{peer.pk}"
    assert services.route(event, "71234") is None
    assert services.route(event, "8") is None
    peer.state = "disabled"
    peer.save()
    assert services.route(event, "81234") is None


def test_route_longest_prefix_wins(event, orga, peer):
    p2 = services.register_peer(event, orga, name="Camp 3", remote_prefix="89", sip_host="x", state="active")
    assert services.route(event, "89111") == f"PJSIP/111@fed-{p2.pk}"
    assert services.route(event, "88111") == f"PJSIP/8111@fed-{peer.pk}"


def test_flag_off(event, peer, settings):
    settings.PET_FEATURES = {**settings.PET_FEATURES, "federation": False}
    assert services.route(event, "81234") is None
    with pytest.raises(services.FederationError):
        services.register_peer(event, None, name="x", remote_prefix="7", sip_host="x")


def test_register_validation(event, orga, peer):
    with pytest.raises(services.FederationError):
        services.register_peer(event, orga, name="dup", remote_prefix="8", sip_host="x")
    with pytest.raises(services.FederationError):
        services.register_peer(event, orga, name="bad", remote_prefix="8a", sip_host="x")


def test_pjsip_config(peer):
    conf = services.pjsip_trunk_config(peer)
    assert f"[fed-{peer.pk}]" in conf and "type=endpoint" in conf and "type=identify" in conf
    assert "media_encryption=sdes" in conf and "transport=transport-tls" in conf
    assert f"outbound_auth=fed-{peer.pk}-auth" in conf and "username=demo" in conf
    assert "contact=sip:pbx.camp2.example:5061;transport=tls" in conf
    peer.srtp = False
    assert "media_encryption=no" in services.pjsip_trunk_config(peer)


def test_publish_and_fetch_directory(event, settings):
    settings.PET_PUBLIC_URL = "https://pet.demo.example"
    event.dial_prefix = "7"
    event.save()
    entry = services.publish_directory_entry(event)
    assert entry["event_slug"] == "demo" and entry["dial_prefix"] == "7" and entry["sip_host"] == "demo.pet.local"

    fake = mock.Mock()
    fake.json.return_value = [entry, {"bogus": True}]
    fake.raise_for_status.return_value = None
    with mock.patch("requests.get", return_value=fake) as get:
        rows = services.fetch_directory("https://pet.demo.example/api/v1/federation/directory/")
    assert get.call_count == 1 and len(rows) == 1
    assert FederationDirectoryEntry.objects.get().event_name == "Demo Camp"

    import requests

    with mock.patch("requests.get", side_effect=requests.ConnectionError("down")):
        assert services.fetch_directory("https://nope.example/") == []


def test_pbx_route_api(client, event, peer, settings):
    settings.PET_PBX_HOOK_SECRET = "s3cret"
    d = client.get("/api/v1/pbx/route/?event=demo&number=81234", HTTP_X_PET_PBX_SECRET="s3cret").json()
    assert d["type"] == "federation" and d["targets"] == [f"PJSIP/1234@fed-{peer.pk}"]


def test_views(client, event, user, orga, member, peer):
    client.force_login(user)
    assert client.get(reverse("federation:index", args=[event.slug])).status_code == 403
    client.force_login(orga)
    r = client.get(reverse("federation:index", args=[event.slug]))
    assert r.status_code == 200 and "Camp 2" in r.content.decode()
    r = client.post(reverse("federation:peer_new", args=[event.slug]), {
        "name": "Camp 3", "remote_prefix": "9", "sip_host": "pbx3", "sip_port": 5061, "transport": "tls",
        "srtp": "on", "state": "active"})
    assert r.status_code == 302, r.content.decode()[:500]
    assert FederationPeer.objects.filter(remote_prefix="9").exists()
    r = client.get(reverse("federation:peer_pjsip", args=[event.slug, peer.pk]))
    assert r.status_code == 200 and b"type=endpoint" in r.content


def test_api(client, event, user, orga, member, peer):
    r = client.get("/api/v1/federation/directory/")
    assert r.status_code == 200 and r.json()[0]["event_slug"] == "demo"
    client.force_login(user)
    assert client.get("/api/v1/federation/peers/?event=demo").status_code == 403
    client.force_login(orga)
    r = client.get("/api/v1/federation/peers/?event=demo").json()
    rows = r["results"] if isinstance(r, dict) else r
    assert len(rows) == 1 and "auth_password" not in rows[0]
    r = client.post("/api/v1/federation/peers/", {"event": "demo", "name": "C4", "remote_prefix": "6", "sip_host": "h"})
    assert r.status_code == 201
    assert client.get(f"/api/v1/federation/peers/{peer.pk}/pjsip/").json()["config"].startswith("; PET federation")
