from django.urls import path

from . import views

app_name = "dect"
PORTAL_MOUNT = True  # mounted at /e/<slug>/dect/

urlpatterns = [
    path("", views.index, name="index"),
    path("handsets/", views.handsets, name="handsets"),
    path("handsets/pool/add/", views.pool_add, name="pool_add"),
    path("handsets/pool/<uuid:pk>/remove/", views.pool_remove, name="pool_remove"),
    path("map/", views.coverage_map, name="map"),
    path("map/<int:map_id>/", views.coverage_map, name="map_detail"),
    path("map/place/", views.map_place, name="map_place"),
    path("alerts/", views.alerts, name="alerts"),
    path("alerts/<int:pk>/resolve/", views.alert_resolve, name="alert_resolve"),
    path("survey/", views.survey, name="survey"),
    path("rfp/<int:pk>/", views.rfp_edit, name="rfp_edit"),
    path("sync/", views.sync_now, name="sync_now"),
]
