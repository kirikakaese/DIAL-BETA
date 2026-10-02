from django.urls import path

from . import views

app_name = "messaging"
PORTAL_MOUNT = True  # mounted at /e/<slug>/messaging/

urlpatterns = [
    path("", views.index, name="index"),
    path("broadcast/", views.broadcast, name="broadcast"),
]
