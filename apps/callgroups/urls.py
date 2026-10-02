from django.urls import path

from . import views

app_name = "callgroups"
PORTAL_MOUNT = True  # mounted at /e/<slug>/callgroups/

urlpatterns = [
    path("", views.index, name="index"),
    path("new/", views.create, name="create"),
    path("mine/", views.mine, name="mine"),
    path("invites/", views.invites, name="invites"),
    path("invites/<str:token>/", views.invite_respond, name="invite_respond"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/delete/", views.delete, name="delete"),
    path("<int:pk>/admins/add/", views.add_admin, name="add_admin"),
    path("<int:pk>/admins/<uuid:upk>/remove/", views.remove_admin, name="remove_admin"),
    path("<int:pk>/invite/", views.invite, name="invite"),
    path("<int:pk>/invites/<int:ipk>/cancel/", views.cancel_invite, name="cancel_invite"),
    path("<int:pk>/members/add/", views.add_member, name="add_member"),
    path("<int:pk>/members/<int:mpk>/remove/", views.remove_member, name="remove_member"),
    path("<int:pk>/members/<int:mpk>/leave/", views.leave, name="leave"),
    path("<int:pk>/members/<int:mpk>/settings/", views.member_settings, name="member_settings"),
    path("<int:pk>/members/<int:mpk>/<slug:action>/", views.toggle, name="toggle"),
]
