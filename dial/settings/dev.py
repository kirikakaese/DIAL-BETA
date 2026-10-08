from .base import *  # noqa: F401,F403
from .base import env

DEBUG = env.bool("DEBUG", default=True)
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=True)
CELERY_TASK_EAGER_PROPAGATES = True
# Local dev without Redis: fall back to in-memory cache
if env.bool("DIAL_LOCAL_CACHE", default=True):
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Behind a forwarding proxy (GitHub Codespaces): trust its X-Forwarded-Proto/-Host so request.scheme and
# the host match the browser (CSRF origin check, absolute links). Never set this for a server reachable directly.
if env.bool("DIAL_TRUST_PROXY_HEADERS", default=False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True
