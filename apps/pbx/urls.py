"""Orga UI of the pbx app: the per-event venue connection page (mounted at /e/<slug>/pbx/)."""
from django.urls import path

from . import views

app_name = "pbx"
PORTAL_MOUNT = True

urlpatterns = [
    path("", views.infrastructure, name="infrastructure"),
]
