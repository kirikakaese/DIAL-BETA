from django.urls import path

from . import views

app_name = "conferences"
PORTAL_MOUNT = True  # mounted at /e/<slug>/conferences/

urlpatterns = [
    path("", views.index, name="index"),
    path("new/", views.new, name="new"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/kick/<int:participant_pk>/", views.kick, name="kick"),
]
