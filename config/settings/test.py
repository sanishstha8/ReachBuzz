"""
Test settings.

The suite must run without any real WhatsApp credentials, so the mock provider
is forced on and Celery runs tasks inline. Safe defaults are injected into the
environment before `base` reads it, so `pytest` works on a clean checkout with
nothing but a reachable PostgreSQL server.
"""

import os
from pathlib import Path

import environ

# Load .env first, then fill only genuine gaps. Doing it the other way round
# would make setdefault() win over the developer's real DATABASE_URL, and the
# suite would silently target the wrong server.
_BASE_DIR = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _BASE_DIR / ".env"
if _ENV_FILE.exists():
    environ.Env.read_env(str(_ENV_FILE))

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-used-anywhere-else")
os.environ.setdefault(
    "DATABASE_URL",
    "postgres://postgres:postgres@localhost:5432/rebuzz",
)
# Tests run against a throwaway database that Django creates as
# test_<NAME>; the developer's data is never touched.
os.environ["DEBUG"] = "False"

from .base import *  # noqa: E402, F403
from .base import MIDDLEWARE, env  # noqa: E402, F401

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

# WhiteNoise expects a collected staticfiles directory that tests do not build.
MIDDLEWARE = [m for m in MIDDLEWARE if "whitenoise" not in m.lower()]

# Never talk to Meta from a test run.
WHATSAPP_PROVIDER = "mock"
META_ACCESS_TOKEN = ""
META_PHONE_NUMBER_ID = "test-phone-number-id"
META_WABA_ID = "test-waba-id"
META_APP_SECRET = "test-app-secret"
META_WEBHOOK_VERIFY_TOKEN = "test-verify-token"
META_API_VERSION = "vTEST"

MOCK_PROVIDER_FAILURE_RATE = 0.0
MOCK_PROVIDER_LATENCY_SECONDS = 0.0
# Off by default so a send test asserts on the send, not on a follow-up
# simulation; the tests that cover callbacks turn it on explicitly.
MOCK_PROVIDER_SIMULATE_CALLBACKS = False
MOCK_PROVIDER_CALLBACK_DELAY = 0

# No Redis in the test environment, and deterministic timing in tests.
WHATSAPP_SEND_RATE_PER_SECOND = 0
WHATSAPP_RATE_LIMIT_BACKEND = "null"
WHATSAPP_MAX_RETRIES = 2
WHATSAPP_RETRY_BACKOFF_SECONDS = 1

# Run Celery tasks synchronously; no broker required. Pinned explicitly rather
# than inherited, so the suite behaves the same whatever a developer's .env
# happens to say about the broker.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_BROKER_TRANSPORT_OPTIONS = {}
CELERY_RESULT_BACKEND = ""

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Throttles would make repeated test requests flaky.
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] = {  # noqa: F405
    "login": None,
    "csv_import": None,
    "campaign_launch": None,
}

LOGGING["root"]["level"] = "ERROR"  # noqa: F405
for _logger in LOGGING["loggers"].values():  # noqa: F405
    _logger["level"] = "ERROR"
