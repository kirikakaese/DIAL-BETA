from django.urls import path

from . import views

app_name = "breakout"
PORTAL_MOUNT = True  # mounted at /e/<slug>/breakout/

urlpatterns = [
    path("", views.index, name="index"),
    path("trunk/new/", views.trunk_edit, name="trunk_new"),
    path("trunk/<int:pk>/", views.trunk_edit, name="trunk_edit"),
    path("trunk/<int:pk>/pjsip/", views.trunk_pjsip, name="trunk_pjsip"),
    path("rule/new/", views.rule_new, name="rule_new"),
    path("rule/<int:pk>/delete/", views.rule_delete, name="rule_delete"),
    path("permission/new/", views.permission_new, name="permission_new"),
    path("permission/<int:pk>/delete/", views.permission_delete, name="permission_delete"),
]
