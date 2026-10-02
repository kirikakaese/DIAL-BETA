"""PWA shell: manifest, service worker, offline page, icons and the base-template hooks."""
import json

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from PIL import Image

from apps.core.context_processors import ASSET_VERSION

pytestmark = pytest.mark.django_db

ICON_DIR = settings.STATICFILES_DIRS[0] / "icons"


def test_manifest_is_valid_and_points_at_existing_icons(client):
    r = client.get(reverse("core:manifest"))
    assert r.status_code == 200
    assert r["Content-Type"].startswith("application/manifest+json")
    assert "max-age=3600" in r["Cache-Control"] and "public" in r["Cache-Control"]
    data = json.loads(r.content)
    for key in ("name", "short_name", "start_url", "scope", "display", "background_color", "theme_color",
                "icons", "shortcuts", "lang"):
        assert key in data, key
    assert data["start_url"] == "/" and data["scope"] == "/" and data["display"] == "standalone"
    assert data["theme_color"] == settings.PET_PWA_THEME_COLOR
    assert data["background_color"] == settings.PET_PWA_BACKGROUND_COLOR
    assert {i["sizes"] for i in data["icons"]} == {"192x192", "512x512"}
    assert any(i["purpose"] == "maskable" for i in data["icons"])
    for icon in data["icons"]:
        assert icon["src"].startswith("/static/icons/")
        assert (ICON_DIR / icon["src"].split("/static/icons/")[1]).exists(), icon["src"]
    assert [s["url"] for s in data["shortcuts"]] == ["/", "/docs/"]


def test_manifest_lang_is_english_regardless_of_browser_language(client):
    r = client.get(reverse("core:manifest"), HTTP_ACCEPT_LANGUAGE="de")
    assert json.loads(r.content)["lang"] == "en"


@pytest.mark.parametrize("name,size,mode", [
    ("pet-192.png", 192, "RGBA"), ("pet-512.png", 512, "RGBA"),
    ("pet-maskable-512.png", 512, "RGBA"), ("apple-touch-icon.png", 180, "RGB"),
])
def test_icons_are_png_of_the_right_size(name, size, mode):
    with Image.open(ICON_DIR / name) as img:
        assert img.format == "PNG"
        assert img.size == (size, size)
        assert img.mode == mode
    assert (ICON_DIR / "favicon.svg").read_text().startswith("<svg")


def test_maskable_icon_is_full_bleed():
    with Image.open(ICON_DIR / "pet-maskable-512.png") as img:
        assert img.getpixel((0, 0))[3] == 255  # corner opaque
    with Image.open(ICON_DIR / "pet-512.png") as img:
        assert img.getpixel((0, 0))[3] == 0  # rounded corner transparent


def test_service_worker_is_served_from_root_with_version_and_lists(client):
    r = client.get(reverse("core:service_worker"))
    assert reverse("core:service_worker") == "/sw.js"
    assert r.status_code == 200
    assert r["Content-Type"].startswith("application/javascript")
    assert r["Service-Worker-Allowed"] == "/"
    assert r["Cache-Control"] == "no-cache"
    body = r.content.decode()
    assert "__PET_ASSET_VERSION__" not in body and "__PET_PRECACHE__" not in body  # both substituted
    assert f'"pet-static-{ASSET_VERSION}"' not in body  # version is concatenated at runtime ...
    assert f'const VERSION = "{ASSET_VERSION}";' in body  # ... from this constant
    precache = json.loads(body.split("const PRECACHE = ", 1)[1].split(";\n", 1)[0])
    assert "/offline/" in precache and "/manifest.webmanifest" in precache
    assert f"/static/css/pet.css?v={ASSET_VERSION}" in precache
    assert f"/static/js/pwa.js?v={ASSET_VERSION}" in precache
    assert "/static/icons/pet-512.png" in precache
    # allowlist + denylist
    for needle in ("phonebook", "docs", "/^\\/e\\/"):
        assert needle in body, needle
    for denied in ("/accounts\\/", "/admin\\/", "/api\\/", "/prov\\/", "orga", "pbx", r"qr\.png", r"\.vcf"):
        assert denied in body, denied
    assert "SKIP_WAITING" in body and "clients.claim" in body and "skipWaiting" in body


def test_service_worker_source_is_not_served_rendered_via_static(client):
    """The raw template under /static/ keeps its placeholders; only /sw.js is registerable."""
    raw = (settings.STATICFILES_DIRS[0] / "js" / "sw.js").read_text()
    assert "__PET_ASSET_VERSION__" in raw and "__PET_PRECACHE__" in raw


def test_offline_page_is_public_and_cacheable(client):
    r = client.get(reverse("core:offline"))
    assert r.status_code == 200
    assert "public" in r["Cache-Control"] and "max-age=300" in r["Cache-Control"]
    assert "no-store" not in r["Cache-Control"]
    assert b"offline" in r.content.lower()
    assert b"data-pwa-reload" in r.content
    assert b'href="/"' in r.content


def test_base_template_has_pwa_hooks(client):
    r = client.get(reverse("portal:home"))
    html = r.content.decode()
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert f'<meta name="theme-color" content="{settings.PET_PWA_THEME_COLOR}">' in html
    assert '<meta name="color-scheme" content="dark light">' in html
    assert 'rel="icon" type="image/svg+xml"' in html
    assert 'rel="apple-touch-icon"' in html
    assert 'name="apple-mobile-web-app-capable" content="yes"' in html
    assert f'/static/js/pwa.js?v={ASSET_VERSION}' in html
    assert f'/static/css/pwa.css?v={ASSET_VERSION}' in html
    assert 'id="pet-pwa"' in html and 'data-sw="/sw.js"' in html
    assert 'id="pet-offline" class="pwa-banner" role="status" aria-live="polite" hidden' in html
    assert 'id="pet-install" hidden' in html


def test_event_pages_use_event_primary_colour_as_theme_color(client, event):
    event.primary_color = "#aa00aa"
    event.save()
    r = client.get(reverse("portal:event_dashboard", args=[event.slug]))
    assert '<meta name="theme-color" content="#aa00aa">' in r.content.decode()


@override_settings(PET_PWA_ENABLED=False)
def test_disabled_pwa_hides_links_and_404s(client):
    assert client.get(reverse("core:service_worker")).status_code == 404
    assert client.get(reverse("core:manifest")).status_code == 404
    assert client.get(reverse("core:offline")).status_code == 200  # harmless, stays reachable
    html = client.get(reverse("portal:home")).content.decode()
    assert 'rel="manifest"' not in html
    assert "data-sw=" not in html
    assert "apple-mobile-web-app-capable" not in html
    assert 'id="pet-pwa"' in html  # offline banner still works via navigator.onLine


def test_asset_version_covers_pwa_files():
    from apps.core.context_processors import VERSIONED_ASSETS

    assert {"css/pwa.css", "js/pwa.js", "js/sw.js"} <= set(VERSIONED_ASSETS)
    for rel in VERSIONED_ASSETS:
        assert (settings.STATICFILES_DIRS[0] / rel).exists(), rel
