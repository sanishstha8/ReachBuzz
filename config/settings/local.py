"""Local development settings."""

from .base import *  # noqa: F403
from .base import BASE_DIR, INSTALLED_APPS, MIDDLEWARE, env  # noqa: F401

DEBUG = True

ALLOWED_HOSTS = env("ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "[::1]"])

# Manifest static storage requires `collectstatic`; plain storage is friendlier
# during development.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# django-debug-toolbar is optional: enable it with DEBUG_TOOLBAR=True.
if env.bool("DEBUG_TOOLBAR", default=False):
    INSTALLED_APPS += ["debug_toolbar"]
    MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")
    INTERNAL_IPS = ["127.0.0.1"]

# Browsable API is convenient locally, but never enabled in production.
REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = [  # noqa: F405
    "rest_framework.renderers.JSONRenderer",
    "rest_framework.renderers.BrowsableAPIRenderer",
]
