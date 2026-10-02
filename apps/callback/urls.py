from django.urls import path

from . import views

app_name = "callback"
PORTAL_MOUNT = True  # mounted at /e/<slug>/callback/

urlpatterns = [
    path("", views.index, name="index"),
    path("request/", views.request_view, name="request"),
    path("cancel/<int:pk>/", views.cancel, name="cancel"),
    path("ringback/", views.ringback, name="ringback"),
    path("wakeup/new/", views.wakeup_new, name="wakeup_new"),
    path("wakeup/<int:pk>/cancel/", views.wakeup_cancel, name="wakeup_cancel"),
    path("wakeup/<int:pk>/snooze/", views.wakeup_snooze, name="wakeup_snooze"),
    path("all/", views.all_view, name="all"),
    path("all/fire/<slug:kind>/<int:pk>/", views.fire_now, name="fire_now"),
]
