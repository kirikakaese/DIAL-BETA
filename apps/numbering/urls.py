from django.urls import path

from . import views

app_name = "numbering"
PORTAL_MOUNT = True  # mounted at /e/<slug>/numbering/

urlpatterns = [
    path("settings/", views.settings_view, name="settings"),
    path("random/", views.random_number, name="random"),
    path("pools/", views.pools, name="pools"),
    path("pools/<int:pk>/delete/", views.pool_delete, name="pool_delete"),
    path("claims/", views.claims, name="claims"),
    path("claims/<int:pk>/resend/", views.claim_resend, name="claim_resend"),
    path("claims/<int:pk>/delete/", views.claim_delete, name="claim_delete"),
    path("claim/<str:token>/", views.claim_redeem, name="claim_redeem"),
]
