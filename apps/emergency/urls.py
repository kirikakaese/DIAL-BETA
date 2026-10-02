from django.urls import path

from . import views

app_name = "emergency"
PORTAL_MOUNT = True  # mounted at /e/<slug>/emergency/

urlpatterns = [
    path("", views.index, name="index"),
    path("target/", views.target_save, name="target_save"),
    path("target/<int:pk>/delete/", views.target_delete, name="target_delete"),
    path("incident/<int:pk>/resolve/", views.incident_resolve, name="incident_resolve"),
    path("broadcast/", views.broadcast, name="broadcast"),
    path("priority/<uuid:pk>/", views.priority, name="priority"),
]
