"""Base settings shared by all environments.

Configuration is read from environment variables (12-factor) so the same
image can run in Docker Compose, Ansible-managed hosts, or a laptop at a
camp with no internet connection.
"""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    DIAL_EARLY_ACCESS_PASSWORD=(str, ""),
    DIAL_EARLY_ACCESS_DAYS=(int, 30),
    DIAL_EARLY_ACCESS_MESSAGE=(str, ""),
    ALLOWED_HOSTS=(list, ["*"]),
    CSRF_TRUSTED_ORIGINS=(list, []),
    DATABASE_URL=(str, f"sqlite:///{BASE_DIR / 'dial.sqlite3'}"),
    REDIS_URL=(str, "redis://localhost:6379/0"),
    TIME_ZONE=(str, "UTC"),
    # PBX
    DIAL_PBX_BACKEND=(str, "apps.pbx.backends.dummy.DummyPBX"),
    ASTERISK_ARI_URL=(str, "http://asterisk:8088/ari"),
    ASTERISK_ARI_USER=(str, "dial"),
    ASTERISK_ARI_PASSWORD=(str, "dial"),
    ASTERISK_ARI_APP=(str, "dial"),
    ASTERISK_AMI_HOST=(str, "asterisk"),
    ASTERISK_AMI_PORT=(int, 5038),
    ASTERISK_AMI_USER=(str, "dial"),
    ASTERISK_AMI_PASSWORD=(str, "dial"),
    ASTERISK_SIP_DOMAIN=(str, "dial.local"),
    # DECT
    DIAL_DECT_BACKEND=(str, "apps.dect.backends.dummy.DummyDECT"),
    OMM_HOST=(str, ""),
    OMM_PORT=(int, 12622),
    OMM_USER=(str, "omm"),
    OMM_PASSWORD=(str, ""),
    OMM_VERIFY_TLS=(bool, False),
    # Alerting
    ALERT_WEBHOOK_URL=(str, ""),
    NTFY_URL=(str, ""),
    ALERT_EMAILS=(list, []),
    # Email
    EMAIL_URL=(str, "consolemail://"),
    DEFAULT_FROM_EMAIL=(str, "dial@example.org"),
    # Feature flags (Section 3 extras)
    DIAL_FEATURES=(list, []),
    DIAL_PUBLIC_URL=(str, "http://localhost:8000"),
    # Registration / e-mail confirmation / abuse protection
    DIAL_REQUIRE_EMAIL_VERIFICATION=(bool, True),
    DIAL_EMAIL_TOKEN_TTL_HOURS=(int, 48),
    DIAL_SPAM_GUARD=(bool, True),
    DIAL_SPAM_GUARD_MIN_SECONDS=(int, 3),
    DIAL_SIGNUP_BLOCKED_DOMAINS=(list, []),
    DIAL_SIGNUP_ALLOWED_DOMAINS=(list, []),
    DIAL_LOGIN_MAX_FAILURES=(int, 5),
    DIAL_LOGIN_IP_MAX_FAILURES=(int, 30),
    DIAL_LOGIN_LOCKOUT_MINUTES=(int, 15),
    # OpenID Connect login
    DIAL_OIDC_ENABLED=(bool, False),
    DIAL_OIDC_ISSUER=(str, ""),
    DIAL_OIDC_CLIENT_ID=(str, ""),
    DIAL_OIDC_CLIENT_SECRET=(str, ""),
    DIAL_OIDC_SCOPES=(str, "openid email profile"),
    DIAL_OIDC_BUTTON_LABEL=(str, "Log in with SSO"),
    DIAL_OIDC_AUTO_CREATE=(bool, True),
    DIAL_OIDC_TRUST_EMAIL_VERIFIED=(bool, True),
    DIAL_OIDC_ALLOW_PASSWORD_LOGIN=(bool, True),
    DIAL_OIDC_USERNAME_CLAIM=(str, "preferred_username"),
    DIAL_OIDC_LOGOUT_AT_IDP=(bool, False),
    DIAL_PBX_OUTBOX_SYNC=(str, ""),
    DIAL_RECORDING_DIR=(str, "/var/spool/asterisk/dial-recordings"),
    # LDAP phonebook server (manage.py dial_ldap)
    DIAL_LDAP_HOST=(str, "0.0.0.0"),
    DIAL_LDAP_PORT=(int, 3890),
    DIAL_LDAP_ALLOW_ANONYMOUS=(bool, False),
    DIAL_LDAP_CACHE_SECONDS=(int, 30),
    # PWA shell (manifest, service worker, offline page)
    DIAL_PWA_ENABLED=(bool, True),
    DIAL_PWA_THEME_COLOR=(str, "#3b82f6"),
    DIAL_PWA_BACKGROUND_COLOR=(str, "#0e1015"),
)

environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="insecure-dev-key-change-me")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")
DIAL_PUBLIC_URL = env("DIAL_PUBLIC_URL")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # third party
    "rest_framework",
    "rest_framework.authtoken",
    "drf_spectacular",
    "django_filters",
    "corsheaders",
    # DIAL apps - ordered roughly by dependency
    "apps.core",
    "apps.accounts",
    "apps.events",
    "apps.numbering",
    "apps.extensions",
    "apps.devices",
    "apps.pbx",
    "apps.dect",
    "apps.callback",
    "apps.phonebook",
    "apps.callgroups",
    "apps.voicemail",
    "apps.stats",
    "apps.messaging",
    "apps.ivr",
    "apps.conferences",
    "apps.federation",
    "apps.breakout",
    "apps.emergency",
    "apps.pages",
    "apps.api",
    "apps.portal",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",

    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.RateLimitMiddleware",
    "apps.core.early_access.EarlyAccessMiddleware",
    "apps.core.middleware.CurrentEventMiddleware",
]

ROOT_URLCONF = "dial.urls"
WSGI_APPLICATION = "dial.wsgi.application"
ASGI_APPLICATION = "dial.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.dial",
            ],
        },
    },
]

DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["ATOMIC_REQUESTS"] = True
DATABASES["default"].setdefault("CONN_MAX_AGE", 60)
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REDIS_URL = env("REDIS_URL")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "portal:dashboard"
LOGOUT_REDIRECT_URL = "portal:home"

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

# The web UI is English only (no translation catalogs are shipped). Announcement / voicemail prompt
# languages played by the PBX are a separate concept: see DIAL_PBX_LANGUAGES.
LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English")]
# Asterisk sound packs available at the venue; offered as event default and per-extension prompt language.
DIAL_PBX_LANGUAGES = [("en", "English"), ("de", "Deutsch")]
TIME_ZONE = env("TIME_ZONE")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
MEDIA_URL = "/media/"
MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "media"))

EMAIL_CONFIG = env.email_url("EMAIL_URL")
vars().update(EMAIL_CONFIG)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL")

# --- Celery -----------------------------------------------------------------
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULE = {
    "dect-poll-infrastructure": {
        "task": "apps.dect.tasks.poll_infrastructure",
        "schedule": 30.0,
    },
    "callback-dispatch-due": {
        "task": "apps.callback.tasks.dispatch_due_callbacks",
        "schedule": 10.0,
    },
    "callback-expire-stale": {
        "task": "apps.callback.tasks.expire_stale_requests",
        "schedule": 60.0,
    },
    "stats-aggregate-hourly": {
        "task": "apps.stats.tasks.aggregate_hourly",
        "schedule": 300.0,
    },
    "stats-enforce-retention": {
        "task": "apps.stats.tasks.enforce_retention",
        "schedule": 3600.0,
    },
    "extensions-expire-guest": {
        "task": "apps.extensions.tasks.expire_temporary_extensions",
        "schedule": 300.0,
    },
    "accounts-purge-email-tokens": {
        "task": "apps.accounts.tasks.purge_email_tokens",
        "schedule": 86400.0,
    },
    "events-apply-scheduled-transitions": {
        "task": "apps.events.tasks.apply_scheduled_transitions",
        "schedule": 60.0,
    },
}

# --- REST framework ---------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.api.authentication.ServiceTokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {"anon": "60/min", "user": "600/min"},
}
SPECTACULAR_SETTINGS = {
    "TITLE": "DIAL - DECT & IP Administration Layer API",
    "DESCRIPTION": "REST API for events, extensions, devices, DECT, callbacks and more.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
}
CORS_ALLOW_ALL_ORIGINS = env.bool("CORS_ALLOW_ALL_ORIGINS", default=False)
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])

# --- DIAL specific -----------------------------------------------------------
# Server-wide fallback backends. Every event normally configures its *own* venue PBX/DECT connection
# (PBXConnection / DECTConnection, orga page "PBX & DECT connection"); these defaults are used for events
# that have none - e.g. the demo event in docker-compose, or a single-event installation.
DIAL_PBX_BACKEND = env("DIAL_PBX_BACKEND")
DIAL_DECT_BACKEND = env("DIAL_DECT_BACKEND")
# Backends an orga may pick per event (key -> dotted path). Operators can add their own subclasses.
DIAL_PBX_BACKENDS = {
    "asterisk": "apps.pbx.backends.asterisk.AsteriskPBX",
    "dummy": "apps.pbx.backends.dummy.DummyPBX",
}
DIAL_DECT_BACKENDS = {
    "omm": "apps.dect.backends.omm.MitelOMM",
    "dummy": "apps.dect.backends.dummy.DummyDECT",
}
ASTERISK = {
    "ARI_URL": env("ASTERISK_ARI_URL"),
    "ARI_USER": env("ASTERISK_ARI_USER"),
    "ARI_PASSWORD": env("ASTERISK_ARI_PASSWORD"),
    "ARI_APP": env("ASTERISK_ARI_APP"),
    "AMI_HOST": env("ASTERISK_AMI_HOST"),
    "AMI_PORT": env("ASTERISK_AMI_PORT"),
    "AMI_USER": env("ASTERISK_AMI_USER"),
    "AMI_PASSWORD": env("ASTERISK_AMI_PASSWORD"),
    "SIP_DOMAIN": env("ASTERISK_SIP_DOMAIN"),
}
OMM = {
    "HOST": env("OMM_HOST"),
    "PORT": env("OMM_PORT"),
    "USER": env("OMM_USER"),
    "PASSWORD": env("OMM_PASSWORD"),
    "VERIFY_TLS": env("OMM_VERIFY_TLS"),
}
ALERTING = {
    "WEBHOOK_URL": env("ALERT_WEBHOOK_URL"),
    "NTFY_URL": env("NTFY_URL"),
    "EMAILS": env("ALERT_EMAILS"),
}

# Feature flags. Must-have features are always on; Section 3 extras can be
# toggled per deployment (and, where it makes sense, per event).
DIAL_ALL_FEATURES = [
    "phonebook", "callgroups", "voicemail", "stats",
    "messaging", "ivr", "conferences", "federation", "breakout",
    "emergency", "guest_extensions", "waitlist", "webhooks",
]
DIAL_DEFAULT_FEATURES = ["phonebook", "callgroups", "voicemail", "stats",
                        "guest_extensions", "waitlist", "webhooks", "emergency"]
_enabled = env("DIAL_FEATURES") or DIAL_DEFAULT_FEATURES
DIAL_FEATURES = {f: (f in _enabled) for f in DIAL_ALL_FEATURES}

# Rate limiting for login / registration / number checks (per IP, per minute)
DIAL_RATE_LIMITS = {
    "login": 20,
    "register": 10,
    "password_reset": 10,
    "availability": 120,
    "early_access": 10,
}
# Early-access gate: a shared password in front of the whole site while a public server is not ready for
# everyone (same contract as EVAC ADR-0012). Empty = off. Changing the password locks everybody out again.
DIAL_EARLY_ACCESS_PASSWORD = env("DIAL_EARLY_ACCESS_PASSWORD")
DIAL_EARLY_ACCESS_DAYS = env("DIAL_EARLY_ACCESS_DAYS")
DIAL_EARLY_ACCESS_MESSAGE = env("DIAL_EARLY_ACCESS_MESSAGE")
# Registration: True = GURU3-style e-mail-first signup (confirm link before the account is created;
# unverified legacy accounts cannot register extensions); False = one-step signup + verification mail.
DIAL_REQUIRE_EMAIL_VERIFICATION = env("DIAL_REQUIRE_EMAIL_VERIFICATION")
# Lifetime of registration / verification / e-mail-change links
DIAL_EMAIL_TOKEN_TTL_HOURS = env("DIAL_EMAIL_TOKEN_TTL_HOURS")
# Spam guard on public forms (signup, login, password reset): honeypot field + minimum fill time
DIAL_SPAM_GUARD = env("DIAL_SPAM_GUARD")
DIAL_SPAM_GUARD_MIN_SECONDS = env("DIAL_SPAM_GUARD_MIN_SECONDS")
# Sign-up e-mail domain policy: block list (e.g. disposable providers) and optional allow list (empty = any)
DIAL_SIGNUP_BLOCKED_DOMAINS = env("DIAL_SIGNUP_BLOCKED_DOMAINS")
DIAL_SIGNUP_ALLOWED_DOMAINS = env("DIAL_SIGNUP_ALLOWED_DOMAINS")
# Login brute-force lockout: failures per account / per IP within 15 minutes, then locked for N minutes
DIAL_LOGIN_MAX_FAILURES = env("DIAL_LOGIN_MAX_FAILURES")
DIAL_LOGIN_IP_MAX_FAILURES = env("DIAL_LOGIN_IP_MAX_FAILURES")
DIAL_LOGIN_LOCKOUT_MINUTES = env("DIAL_LOGIN_LOCKOUT_MINUTES")
# OpenID Connect login (authorization code + PKCE, discovery via <issuer>/.well-known/openid-configuration).
# Redirect URI to register at the IdP: <DIAL_PUBLIC_URL>/accounts/oidc/callback/  (see: manage.py dial_oidc_check)
DIAL_OIDC_ENABLED = env("DIAL_OIDC_ENABLED")
DIAL_OIDC_ISSUER = env("DIAL_OIDC_ISSUER")
DIAL_OIDC_CLIENT_ID = env("DIAL_OIDC_CLIENT_ID")
DIAL_OIDC_CLIENT_SECRET = env("DIAL_OIDC_CLIENT_SECRET")
DIAL_OIDC_SCOPES = env("DIAL_OIDC_SCOPES")
DIAL_OIDC_BUTTON_LABEL = env("DIAL_OIDC_BUTTON_LABEL")
# Create a DIAL account on first SSO login (otherwise only pre-existing / linked accounts may log in)
DIAL_OIDC_AUTO_CREATE = env("DIAL_OIDC_AUTO_CREATE")
# Take email_verified from the IdP claims (skips DIAL's own e-mail confirmation for verified addresses)
DIAL_OIDC_TRUST_EMAIL_VERIFIED = env("DIAL_OIDC_TRUST_EMAIL_VERIFIED")
# False = SSO only: password form, signup and reset links are hidden (superusers: /admin/login/)
DIAL_OIDC_ALLOW_PASSWORD_LOGIN = env("DIAL_OIDC_ALLOW_PASSWORD_LOGIN")
# Claim used as nickname for auto-created accounts (fallback: local part of the e-mail address)
DIAL_OIDC_USERNAME_CLAIM = env("DIAL_OIDC_USERNAME_CLAIM")
# Also end the IdP session on logout (RP-initiated logout via end_session_endpoint)
DIAL_OIDC_LOGOUT_AT_IDP = env("DIAL_OIDC_LOGOUT_AT_IDP")
# PBX outbox delivery: "" = follow CELERY_TASK_ALWAYS_EAGER (sync without a worker), "true"/"false" to force
_outbox_sync = env("DIAL_PBX_OUTBOX_SYNC").strip().lower()
DIAL_PBX_OUTBOX_SYNC = None if _outbox_sync == "" else _outbox_sync in ("1", "true", "yes", "on")
# SIP credential policy
DIAL_SIP_PASSWORD_LENGTH = 24
# Digits in the per-extension DECT claim code (dialled after the plan's claim number to bind a handset)
# and in the announcement recording code (dialled after the plan's recording number).
DIAL_DECT_CLAIM_CODE_LENGTH = 6
# Where Asterisk writes announcements recorded by phone (``announcement-record-start`` hook returns
# ``<dir>/<event>-<number>-<stamp>``). Share this directory with the Asterisk container (docker-compose volume
# ``recordings``); DIAL imports the wav into MEDIA_ROOT/ivr/<slug>/<number>/ when it can read it.
DIAL_RECORDING_DIR = env("DIAL_RECORDING_DIR")
# Read-only LDAP phonebook for desk phones / the DECT OMM (``manage.py dial_ldap``, apps.phonebook.ldap).
# Bind DN cn=directory,dc=<slug>,dc=dial with the event's directory token; anonymous binds are off by default.
DIAL_LDAP_HOST = env("DIAL_LDAP_HOST")
DIAL_LDAP_PORT = env("DIAL_LDAP_PORT")
DIAL_LDAP_ALLOW_ANONYMOUS = env("DIAL_LDAP_ALLOW_ANONYMOUS")
DIAL_LDAP_CACHE_SECONDS = env("DIAL_LDAP_CACHE_SECONDS")
# Installable web app: /manifest.webmanifest + /sw.js (service worker; browsers require HTTPS or localhost).
# False hides the manifest link, skips SW registration and answers 404 on both URLs (installed SWs unregister).
DIAL_PWA_ENABLED = env("DIAL_PWA_ENABLED")
# Manifest colours; defaults match dial.css (--primary / dark --bg). Event pages use the event's primary colour.
DIAL_PWA_THEME_COLOR = env("DIAL_PWA_THEME_COLOR")
DIAL_PWA_BACKGROUND_COLOR = env("DIAL_PWA_BACKGROUND_COLOR")
# Callback defaults
DIAL_CALLBACK_DEFAULT_TTL_MINUTES = 30
DIAL_TEST_RINGBACK_DELAY_SECONDS = 10
# CDR retention (days); 0 = keep forever
DIAL_CDR_RETENTION_DAYS = env.int("DIAL_CDR_RETENTION_DAYS", default=30)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"std": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "std"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {"dial": {"level": "DEBUG" if DEBUG else "INFO"}},
}
