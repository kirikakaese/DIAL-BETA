from .base import *  # noqa: F401,F403

DEBUG = False
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
DIAL_PBX_BACKEND = "apps.pbx.backends.dummy.DummyPBX"
DIAL_DECT_BACKEND = "apps.dect.backends.dummy.DummyDECT"
# Tests exercise both signup modes explicitly via override_settings; default to the one-step flow and
# disable the form spam guard (honeypot/timing) so plain client.post() works. Dedicated tests turn it on.
DIAL_REQUIRE_EMAIL_VERIFICATION = False
DIAL_SPAM_GUARD = False
# OIDC tests enable SSO explicitly via override_settings (and mock ``requests``).
DIAL_OIDC_ENABLED = False
DIAL_FEATURES = {f: True for f in DIAL_ALL_FEATURES}  # noqa: F405
WHITENOISE_AUTOREFRESH = True
WHITENOISE_USE_FINDERS = True
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
