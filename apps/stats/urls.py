from django.urls import path

from . import views

app_name = "stats"
PORTAL_MOUNT = True  # mounted at /e/<slug>/stats/

urlpatterns = [
    path("", views.index, name="index"),
    path("export/csv/", views.export_csv, name="export_csv"),
    path("mine/", views.mine, name="mine"),
    path("mine/export/", views.gdpr_export, name="gdpr_export"),
    path("mine/delete/", views.gdpr_delete, name="gdpr_delete"),
]
