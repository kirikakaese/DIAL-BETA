"""Core views: the PWA shell - web app manifest, service worker and offline page.

The service worker source lives in ``static/js/sw.js`` but must be served from the site root so its scope
can be ``/``. The view substitutes two placeholders (``__DIAL_ASSET_VERSION__`` and ``__DIAL_PRECACHE__``),
so the cache name changes whenever the static bundle changes (see ``context_processors.ASSET_VERSION``).
"""
from __future__ import annotations

import json

from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.urls import reverse
from django.utils.cache import patch_cache_control
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from .context_processors import ASSET_VERSION

# (path under static/, sizes, purpose) - all PNG
MANIFEST_ICONS = (
    ("icons/dial-192.png", "192x192", "any"),
    ("icons/dial-512.png", "512x512", "any"),
    ("icons/dial-maskable-512.png", "512x512", "maskable"),
)
# Precached on service-worker install (plus the offline page and the manifest).
PRECACHE_VERSIONED = ("css/dial.css", "js/dial.js", "css/pwa.css", "js/pwa.js", "icons/favicon.svg")
PRECACHE_PLAIN = ("icons/dial-192.png", "icons/dial-512.png", "icons/dial-maskable-512.png",
                  "icons/apple-touch-icon.png")
SW_SOURCE = "js/sw.js"


def _require_pwa() -> None:
    if not getattr(settings, "DIAL_PWA_ENABLED", True):
        raise Http404


def _sw_source() -> str:
    for base in settings.STATICFILES_DIRS:
        path = base / SW_SOURCE
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise Http404


def precache_urls() -> list[str]:
    urls = [reverse("core:offline"), reverse("core:manifest")]
    urls += [f"{static(rel)}?v={ASSET_VERSION}" for rel in PRECACHE_VERSIONED]
    urls += [static(rel) for rel in PRECACHE_PLAIN]
    return urls


@require_safe
def manifest(request):
    """One global manifest (``/manifest.webmanifest``); event colours only affect ``<meta theme-color>``."""
    _require_pwa()
    data = {
        "id": "/",
        "name": "DIAL – DECT & IP Administration Layer",
        "short_name": "DIAL",
        "description": _("Your phone number for the event: extensions, handsets, softphones and the phonebook."),
        "lang": settings.LANGUAGE_CODE,
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": settings.DIAL_PWA_BACKGROUND_COLOR,
        "theme_color": settings.DIAL_PWA_THEME_COLOR,
        "icons": [
            {"src": static(rel), "sizes": sizes, "type": "image/png", "purpose": purpose}
            for rel, sizes, purpose in MANIFEST_ICONS
        ],
        "shortcuts": [
            {"name": _("My extensions"), "url": "/", "icons": [{"src": static("icons/dial-192.png"),
                                                                 "sizes": "192x192", "type": "image/png"}]},
            {"name": _("Docs"), "url": "/docs/"},
        ],
        "categories": ["utilities", "productivity"],
    }
    resp = JsonResponse(data, content_type="application/manifest+json",
                        json_dumps_params={"ensure_ascii": False, "indent": 1})
    patch_cache_control(resp, public=True, max_age=3600)
    return resp


@require_safe
def service_worker(request):
    """``/sw.js`` - never HTTP-cached so browsers pick up a new ASSET_VERSION on the next navigation."""
    _require_pwa()
    body = (_sw_source()
            .replace("__DIAL_ASSET_VERSION__", ASSET_VERSION)
            .replace("__DIAL_PRECACHE__", json.dumps(precache_urls())))
    resp = HttpResponse(body, content_type="application/javascript; charset=utf-8")
    resp["Service-Worker-Allowed"] = "/"
    resp["Cache-Control"] = "no-cache"
    return resp


@require_safe
def offline(request):
    """Fallback page the service worker shows when a navigation fails; precached, so it must be cacheable."""
    resp = render(request, "offline.html")
    patch_cache_control(resp, public=True, max_age=300)
    return resp
