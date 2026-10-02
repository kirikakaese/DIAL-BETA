from django.urls import path

from . import views

app_name = "devices"
PORTAL_MOUNT = True  # mounted at /e/<slug>/devices/ - keep clear of portal's <uuid:pk>/ paths

urlpatterns = [
    path("history/", views.history, name="history"),
    path("reuse/<uuid:pk>/", views.reuse, name="reuse"),
    path("vendor/<uuid:pk>/", views.suggest_vendor, name="suggest_vendor"),
    path("manufacturers/", views.manufacturers, name="manufacturers"),
    path("gsm/", views.gsm, name="gsm"),
    path("gsm/<uuid:pk>/token/", views.gsm_token, name="gsm_token"),
]
