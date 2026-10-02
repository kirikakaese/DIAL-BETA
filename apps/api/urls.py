"""REST API v1 root.

Each DIAL app may expose an ``api.py`` module with::

    def register(router):           # add DRF viewsets
        router.register("things", ThingViewSet, basename="thing")

    urlpatterns = [...]             # optional extra non-viewset routes

They are discovered automatically here so apps stay decoupled.
"""
from importlib import import_module

from django.apps import apps as django_apps
from django.urls import include, path
from rest_framework.routers import DefaultRouter

app_name = "api"

router = DefaultRouter()
extra_patterns = []

for cfg in django_apps.get_app_configs():
    if not cfg.name.startswith("apps."):
        continue
    try:
        mod = import_module(f"{cfg.name}.api")
    except ModuleNotFoundError as exc:
        if exc.name != f"{cfg.name}.api":
            raise
        continue
    if hasattr(mod, "register"):
        mod.register(router)
    if hasattr(mod, "urlpatterns"):
        extra_patterns.extend(mod.urlpatterns)

urlpatterns = [path("", include(router.urls))] + extra_patterns
