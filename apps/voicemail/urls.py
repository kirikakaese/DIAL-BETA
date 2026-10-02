from django.urls import path

from . import views

app_name = "voicemail"
PORTAL_MOUNT = True  # mounted at /e/<slug>/voicemail/

urlpatterns = [
    path("", views.index, name="index"),
    path("settings/<uuid:ext_pk>/", views.settings_view, name="settings"),
    path("message/<int:pk>/audio/", views.audio, name="audio"),
    path("message/<int:pk>/read/", views.mark_read, name="mark_read"),
    path("message/<int:pk>/delete/", views.delete, name="delete"),
    path("all/", views.all_view, name="all"),
]
