from django.urls import path

from . import views

app_name = "ivr"
PORTAL_MOUNT = True  # mounted at /e/<slug>/ivr/

urlpatterns = [
    path("", views.index, name="index"),
    path("announcement/new/", views.announcement_edit, name="announcement_new"),
    path("announcement/<int:pk>/edit/", views.announcement_edit, name="announcement_edit"),
    path("announcement/<int:pk>/record-code/", views.announcement_new_record_code, name="announcement_new_record_code"),
    path("menu/new/", views.menu_edit, name="menu_new"),
    path("menu/<int:pk>/edit/", views.menu_edit, name="menu_edit"),
]
