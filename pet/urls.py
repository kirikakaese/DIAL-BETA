"""Root URL configuration."""
from importlib import import_module

from django.apps import apps as django_apps
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls", namespace="accounts")),
    path("api/v1/", include("apps.api.urls", namespace="api")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]

# Feature app UIs: apps/<app>/urls.py with ``app_name`` and ``PORTAL_MOUNT = True``
# are mounted at /e/<slug>/<label>/ as top-level namespaces (e.g. ``phonebook:index``).
for _cfg in django_apps.get_app_configs():
    if not _cfg.name.startswith("apps.") or _cfg.label in ("portal", "api", "accounts"):
        continue
    try:
        _mod = import_module(f"{_cfg.name}.urls")
    except ModuleNotFoundError as exc:
        if exc.name != f"{_cfg.name}.urls":
            raise
        continue
    if getattr(_mod, "PORTAL_MOUNT", False):
        urlpatterns.append(
            path(f"e/<slug:slug>/{_cfg.label}/", include(f"{_cfg.name}.urls", namespace=_cfg.label))
        )

# Phone autoprovisioning + GSM core hook (token/secret protected, no login)
urlpatterns.append(path("prov/", include("apps.devices.prov_urls")))

# PWA shell: /manifest.webmanifest, /sw.js (root scope), /offline/
urlpatterns.append(path("", include("apps.core.urls", namespace="core")))
urlpatterns.append(path("", include("apps.portal.urls", namespace="portal")))

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
