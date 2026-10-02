"""Account URLs: login, registration (incl. e-mail confirmation), profile, tokens, GDPR, passwords."""
from django.contrib.auth import views as auth_views
from django.urls import path

from . import oidc_views, views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", oidc_views.LogoutView.as_view(), name="logout"),
    path("oidc/login/", oidc_views.oidc_login, name="oidc_login"),
    path("oidc/callback/", oidc_views.oidc_callback, name="oidc_callback"),
    path("oidc/unlink/", oidc_views.oidc_unlink, name="oidc_unlink"),
    path("register/", views.register, name="register"),
    path("register/confirm/<str:token>/", views.register_confirm, name="register_confirm"),
    path("verify/<str:token>/", views.verify_email, name="verify_email"),
    path("profile/", views.profile, name="profile"),
    path("profile/verify/resend/", views.resend_verification, name="resend_verification"),
    path("profile/email/", views.change_email, name="change_email"),
    path("profile/email/confirm/<str:token>/", views.change_email_confirm, name="change_email_confirm"),
    path("profile/tokens/", views.tokens, name="tokens"),
    path("profile/export/", views.gdpr_export, name="gdpr_export"),
    path("profile/delete/", views.gdpr_delete, name="gdpr_delete"),
    path("password/change/", views.PasswordChangeView.as_view(), name="password_change"),
    path("password/reset/", views.PasswordResetView.as_view(), name="password_reset"),
    path("password/reset/done/", auth_views.PasswordResetDoneView.as_view(
        template_name="accounts/password_reset_done.html"), name="password_reset_done"),
    path("password/reset/<uidb64>/<token>/", views.PasswordResetConfirmView.as_view(),
         name="password_reset_confirm"),
    path("password/reset/complete/", auth_views.PasswordResetCompleteView.as_view(
        template_name="accounts/password_reset_complete.html"), name="password_reset_complete"),
]
