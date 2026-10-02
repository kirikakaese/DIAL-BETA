"""Portal views: orga sees pages, plain users get 403, map placement + resolve + sync actions."""
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.dect import services
from apps.dect.models import RFP, Alert, SiteSurveyLog, VenueMap
from apps.dect.provisioning import provision_dect_for_extension
from apps.dect.reverse import dect_reverse

pytestmark = [pytest.mark.django_db, pytest.mark.urls("apps.dect.tests.urls_stub")]

PAGES = ["index", "handsets", "map", "alerts", "survey"]


@pytest.fixture
def orga_client(orga):
    c = Client()
    c.force_login(orga)
    return c


@pytest.fixture
def synced(dect, event, dect_extension, dect_device):
    provision_dect_for_extension(dect_extension)
    dect.set_rfp("2", connected=False)
    services.sync_infrastructure(event)
    return event


@pytest.mark.parametrize("name", PAGES)
def test_pages_ok_for_orga(orga_client, synced, name):
    r = orga_client.get(dect_reverse(name, kwargs={"slug": "demo"}))
    assert r.status_code == 200
    assert b"dect/" in r.content or b"DECT" in r.content


@pytest.mark.parametrize("name", PAGES)
def test_pages_forbidden_for_plain_user(user, member, event, name):
    c = Client()
    c.force_login(user)
    assert c.get(dect_reverse(name, kwargs={"slug": "demo"})).status_code == 403


def test_anonymous_redirect_or_forbidden(event):
    r = Client().get(dect_reverse("index", kwargs={"slug": "demo"}))
    assert r.status_code in (302, 403)


def test_index_shows_rfps_and_alerts(orga_client, synced):
    r = orga_client.get(dect_reverse("index", kwargs={"slug": "demo"}))
    html = r.content.decode()
    assert "RFP-Foodcourt" in html and "rfp.down" in html and 'data-refresh="15"' in html


def test_handsets_search(orga_client, synced, dect_device):
    url = dect_reverse("handsets", kwargs={"slug": "demo"})
    assert "0123456789012" in orga_client.get(url, {"q": "4242"}).content.decode()
    assert "0123456789012" not in orga_client.get(url, {"q": "nomatch"}).content.decode()


def test_rfp_edit(orga_client, synced):
    rfp = RFP.objects.get(event=synced, omm_id="1")
    url = dect_reverse("rfp_edit", kwargs={"slug": "demo", "pk": rfp.pk})
    assert orga_client.get(url).status_code == 200
    r = orga_client.post(url, {"name": "Stage Left", "location": "Truss", "cluster": rfp.cluster_id,
                               "is_active": "on"})
    assert r.status_code == 302
    rfp.refresh_from_db()
    assert rfp.name == "Stage Left" and rfp.location == "Truss"


def test_alert_resolve_and_sync_now(orga_client, synced):
    alert = Alert.objects.get(kind="rfp.down", resolved_at__isnull=True)
    r = orga_client.post(dect_reverse("alert_resolve", kwargs={"slug": "demo", "pk": alert.pk}))
    assert r.status_code == 302
    alert.refresh_from_db()
    assert alert.resolved_at is not None
    r = orga_client.post(dect_reverse("sync_now", kwargs={"slug": "demo"}), follow=True)
    assert r.status_code == 200 and "Synced 6 RFPs" in r.content.decode()


def _png():
    import struct
    import zlib

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    raw = b"\x00\xff\x00\x00\x00"
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    return io.BytesIO(data).getvalue()


def test_map_upload_and_place(orga_client, synced, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    url = dect_reverse("map", kwargs={"slug": "demo"})
    r = orga_client.post(url, {"name": "Ground floor", "is_default": "on",
                               "image": SimpleUploadedFile("plan.png", _png(), content_type="image/png")})
    assert r.status_code == 302
    vm = VenueMap.objects.get(event=synced)
    assert vm.width == 1 and vm.is_default
    r = orga_client.get(url)
    assert r.status_code == 200 and "venue-map" in r.content.decode()
    rfp = RFP.objects.get(event=synced, omm_id="1")
    r = orga_client.post(dect_reverse("map_place", kwargs={"slug": "demo"}),
                         {"rfp": rfp.pk, "map": vm.pk, "x": "33.3", "y": "120"})
    assert r.status_code == 200 and r.json()["y"] == 100.0
    rfp.refresh_from_db()
    assert rfp.venue_map == vm and rfp.pos_x == 33.3
    assert "rfp-pin" in orga_client.get(url).content.decode()
    r = orga_client.post(dect_reverse("map_place", kwargs={"slug": "demo"}),
                         {"rfp": rfp.pk, "map": vm.pk, "x": "0", "y": "0", "remove": "1"})
    rfp.refresh_from_db()
    assert rfp.venue_map is None and rfp.pos_x is None
    assert orga_client.post(dect_reverse("map_place", kwargs={"slug": "demo"}), {"rfp": "x"}).status_code == 400


def test_survey_page_lists_logs(orga_client, synced):
    services.log_site_survey(synced, "4242")
    assert SiteSurveyLog.objects.count() == 1
    html = orga_client.get(dect_reverse("survey", kwargs={"slug": "demo"})).content.decode()
    assert "9002" in html and "0123456789012" in html
