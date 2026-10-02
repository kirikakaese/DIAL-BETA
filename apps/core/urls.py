"""Root-level core URLs (PWA shell). Mounted at ``/`` by ``pet.urls``; not a portal feature app."""
from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("manifest.webmanifest", views.manifest, name="manifest"),
    path("sw.js", views.service_worker, name="service_worker"),
    path("offline/", views.offline, name="offline"),
]
