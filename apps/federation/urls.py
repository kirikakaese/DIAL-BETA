from django.urls import path

from . import views

app_name = "federation"
PORTAL_MOUNT = True  # mounted at /e/<slug>/federation/

urlpatterns = [
    path("", views.index, name="index"),
    path("peer/new/", views.peer_edit, name="peer_new"),
    path("peer/<int:pk>/", views.peer_edit, name="peer_edit"),
    path("peer/<int:pk>/pjsip/", views.peer_pjsip, name="peer_pjsip"),
    path("directory/fetch/", views.directory_fetch, name="directory_fetch"),
]
