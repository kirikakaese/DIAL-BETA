from django.urls import path, re_path

from . import views

app_name = "phonebook"
PORTAL_MOUNT = True  # mounted at /e/<slug>/phonebook/

urlpatterns = [
    path("", views.index, name="index"),
    path("export.pdf", views.pdf, name="pdf"),
    path("export.csv", views.csv_export, name="csv"),
    path("export.vcf", views.vcf, name="vcf"),
    path("export.ldif", views.ldif, name="ldif"),
    path("export.json", views.json_export, name="json"),
    path("settings/", views.settings_view, name="settings"),
    path("settings/rotate-token/", views.rotate_directory_token, name="rotate_directory_token"),
    # remote directory for desk phones / OMM (feature #9): the token is the credential, no login
    re_path(r"^remote/(?P<token>[A-Za-z0-9_\-]{16,64})/(?P<vendor>[a-z]+)\.xml$", views.remote_directory,
            name="remote_directory"),
    # business card of one extension (feature #20); numbers are digits (plus */+ feature-code style)
    re_path(r"^(?P<number>[0-9*+]+)\.vcf$", views.vcard_one, name="vcard_one"),
    re_path(r"^(?P<number>[0-9*+]+)/qr\.png$", views.card_qr, name="card_qr"),
    re_path(r"^(?P<number>[0-9*+]+)/card/$", views.card_print, name="card_print"),
]
