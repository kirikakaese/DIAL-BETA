"""Info pages UI (mounted at /e/<slug>/pages/)."""
from django.urls import path

from . import views

app_name = "pages"
PORTAL_MOUNT = True

urlpatterns = [
    path("", views.index, name="index"),
    path("manage/", views.manage, name="manage"),
    path("manage/new/", views.create, name="create"),
    path("manage/<slug:page_slug>/", views.edit, name="edit"),
    path("manage/<slug:page_slug>/delete/", views.delete, name="delete"),
    path("<slug:page_slug>/", views.show, name="show"),
]
