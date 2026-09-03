"""
Base settings shared by every environment.

Environment-specific modules (local.py, production.py, test.py) import * from
here and override only what differs. No secret is ever hardcoded: every value
that varies by deployment is read from the environment via django-environ.
"""

from pathlib import Path

import environ

# BASE_DIR points at the repository root (the directory holding manage.py).
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    CSRF_TRUSTED_ORIGINS=(list, []),
)

# Read .env if present. In production, real environment variables are preferred
# and take precedence over anything in the file.
env_file = BASE_DIR / ".env"
if env_file.exists():
    env.read_env(str(env_file))


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

AUTH_USER_MODEL = "accounts.User"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "django_filters",
    "drf_spectacular",
]

LOCAL_APPS = [
    "core.apps.CoreConfig",
    "organizations.apps.OrganizationsConfig",
    "billing.apps.BillingConfig",
    "accounts.apps.AccountsConfig",
    "contacts.apps.ContactsConfig",
    "whatsapp.apps.WhatsappConfig",
    "campaigns.apps.CampaignsConfig",
    "messaging.apps.MessagingConfig",
    "dashboard.apps.DashboardConfig",
    "pages.apps.PagesConfig",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.site_context",
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASES = {
    "default": env.db("DATABASE_URL"),
}
DATABASES["default"]["ATOMIC_REQUESTS"] = False
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Brute-force protection for the sign-in *form*. The REST login is throttled
# separately by DEFAULT_THROTTLE_RATES["login"]; both doors need a lock.
# Counted per client address rather than per account, so that knowing an
# operator's email address is not enough to lock them out. Set the limit to 0
# to disable.
LOGIN_ATTEMPT_LIMIT = env.int("LOGIN_ATTEMPT_LIMIT", default=10)
LOGIN_ATTEMPT_WINDOW_SECONDS = env.int("LOGIN_ATTEMPT_WINDOW_SECONDS", default=300)

# Registration writes rows and mails an address the caller supplies, so it is
# both a way to fill the database and a way to point our mail server at someone
# who never asked for it. Counted per network on accounts actually created.
SIGNUP_LIMIT = env.int("SIGNUP_LIMIT", default=5)
SIGNUP_WINDOW_SECONDS = env.int("SIGNUP_WINDOW_SECONDS", default=3600)

# Password reset and re-sending a confirmation link both mail on demand, which
# makes either one a way to bury an inbox using our sending reputation.
OUTBOUND_EMAIL_LIMIT = env.int("OUTBOUND_EMAIL_LIMIT", default=5)
OUTBOUND_EMAIL_WINDOW_SECONDS = env.int("OUTBOUND_EMAIL_WINDOW_SECONDS", default=900)

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "dashboard:home"
LOGOUT_REDIRECT_URL = "accounts:login"

# Sessions expire after a period of inactivity rather than lasting forever.
SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE", default=60 * 60 * 8)
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = False  # read by the fetch() helper in static/js/api.js
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"


# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------

LANGUAGE_CODE = env("LANGUAGE_CODE", default="en-us")
TIME_ZONE = env("TIME_ZONE", default="UTC")
USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------------
# Static and media files
# ---------------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}


# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "core.pagination.StandardResultsPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "EXCEPTION_HANDLER": "core.exceptions.api_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "login": env("THROTTLE_LOGIN", default="10/min"),
        "csv_import": env("THROTTLE_CSV_IMPORT", default="10/hour"),
        "campaign_launch": env("THROTTLE_CAMPAIGN_LAUNCH", default="30/hour"),
    },
}

SPECTACULAR_SETTINGS = {
    "TITLE": "ReachBuzz API",
    "DESCRIPTION": (
        "ReachBuzz — Business Messaging & Automation Platform.\n\n"
        "A WhatsApp Business messaging and campaign management platform for "
        "businesses to send, manage, and track customer communications.\n\n"
        "Messages are sent only to recipients whose consent is recorded, through "
        "the official Meta WhatsApp Business Platform Cloud API."
    ),
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    # Several models have a "status" field with different choice sets. Naming
    # each one explicitly keeps the generated schema readable instead of
    # producing components like "Status109Enum".
    "ENUM_NAME_OVERRIDES": {
        "ContactStatusEnum": "contacts.models.ContactStatus.choices",
        "ContactImportStatusEnum": "contacts.models.ImportStatus.choices",
        "ImportRowOutcomeEnum": "contacts.models.RowOutcome.choices",
        "OptInSourceEnum": "contacts.models.OptInSource.choices",
        "OptOutSourceEnum": "contacts.models.OptOutSource.choices",
        "UserRoleEnum": "accounts.models.UserRole.choices",
        "CampaignStatusEnum": "campaigns.models.CampaignStatus.choices",
        "CampaignMessageTypeEnum": "campaigns.models.CampaignMessageType.choices",
        "MessageStatusEnum": "messaging.models.MessageStatus.choices",
        "StatusEventSourceEnum": "messaging.models.StatusEventSource.choices",
        "TemplateStatusEnum": "whatsapp.models.TemplateStatus.choices",
        "TemplateCategoryEnum": "whatsapp.models.TemplateCategory.choices",
        "TemplateSourceEnum": "whatsapp.models.TemplateSource.choices",
    },
}


# ---------------------------------------------------------------------------
# Celery (fully wired up in Phase 5)
# ---------------------------------------------------------------------------

REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default=REDIS_URL)
# Transport-specific options, as JSON. Redis deployments sometimes need a
# visibility timeout here; the filesystem transport needs its spool
# directories. Empty for a default Redis setup.
CELERY_BROKER_TRANSPORT_OPTIONS = env.json("CELERY_BROKER_TRANSPORT_OPTIONS", default={})
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="")
CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", default=False)
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = env.int("CELERY_TASK_TIME_LIMIT", default=300)
CELERY_TASK_SOFT_TIME_LIMIT = env.int("CELERY_TASK_SOFT_TIME_LIMIT", default=240)
CELERY_TIMEZONE = TIME_ZONE
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True


# ---------------------------------------------------------------------------
# WhatsApp provider configuration (consumed from Phase 5 onward)
#
# Credentials live only in the environment. They are never logged, never
# rendered in a template, and never returned by the API.
# ---------------------------------------------------------------------------

WHATSAPP_PROVIDER = env("WHATSAPP_PROVIDER", default="mock")

META_API_VERSION = env("META_API_VERSION", default="")
META_ACCESS_TOKEN = env("META_ACCESS_TOKEN", default="")
META_PHONE_NUMBER_ID = env("META_PHONE_NUMBER_ID", default="")
META_WABA_ID = env("META_WABA_ID", default="")
META_APP_ID = env("META_APP_ID", default="")
META_APP_SECRET = env("META_APP_SECRET", default="")
META_WEBHOOK_VERIFY_TOKEN = env("META_WEBHOOK_VERIFY_TOKEN", default="")

# Outbound throughput ceiling we impose on ourselves so we stay comfortably
# inside the messaging limits of the connected WhatsApp Business Account.
WHATSAPP_SEND_RATE_PER_SECOND = env.int("WHATSAPP_SEND_RATE_PER_SECOND", default=10)
WHATSAPP_MAX_RETRIES = env.int("WHATSAPP_MAX_RETRIES", default=3)
WHATSAPP_RETRY_BACKOFF_SECONDS = env.int("WHATSAPP_RETRY_BACKOFF_SECONDS", default=10)
WHATSAPP_REQUEST_TIMEOUT = env.int("WHATSAPP_REQUEST_TIMEOUT", default=30)

# "redis" uses the shared token bucket; "null" disables limiting entirely,
# which is what the test settings use so the suite needs no Redis.
WHATSAPP_RATE_LIMIT_BACKEND = env("WHATSAPP_RATE_LIMIT_BACKEND", default="redis")

# Mock provider behaviour, used for local development and tests.
MOCK_PROVIDER_FAILURE_RATE = env.float("MOCK_PROVIDER_FAILURE_RATE", default=0.0)
MOCK_PROVIDER_LATENCY_SECONDS = env.float("MOCK_PROVIDER_LATENCY_SECONDS", default=0.0)
# After a simulated send, stand in for the provider's delivery webhooks so the
# full lifecycle can be demonstrated. Recorded as SIMULATED, never as a webhook.
MOCK_PROVIDER_SIMULATE_CALLBACKS = env.bool("MOCK_PROVIDER_SIMULATE_CALLBACKS", default=True)
MOCK_PROVIDER_CALLBACK_DELAY = env.int("MOCK_PROVIDER_CALLBACK_DELAY", default=3)
MOCK_PROVIDER_READ_RATE = env.float("MOCK_PROVIDER_READ_RATE", default=0.6)


# ---------------------------------------------------------------------------
# Contacts / campaigns domain configuration
# ---------------------------------------------------------------------------

DEFAULT_COUNTRY_CODE = env("DEFAULT_COUNTRY_CODE", default="NP")
CSV_IMPORT_MAX_BYTES = env.int("CSV_IMPORT_MAX_BYTES", default=5 * 1024 * 1024)
CSV_IMPORT_MAX_ROWS = env.int("CSV_IMPORT_MAX_ROWS", default=20_000)
CAMPAIGN_MAX_RECIPIENTS = env.int("CAMPAIGN_MAX_RECIPIENTS", default=5_000)


# ---------------------------------------------------------------------------
# Branding shown in the UI (see core.context_processors)
# ---------------------------------------------------------------------------

# Short name for UI chrome (sidebar, page titles); full name and tagline for
# documentation, the API schema and the page description.
SITE_NAME = env("SITE_NAME", default="ReachBuzz")
SITE_TAGLINE = env("SITE_TAGLINE", default="Business Messaging & Automation Platform")
SITE_DESCRIPTION = env(
    "SITE_DESCRIPTION",
    default=(
        "A WhatsApp Business messaging and campaign management platform for "
        "businesses to send, manage, and track customer communications."
    ),
)
BUSINESS_DISPLAY_NAME = env("BUSINESS_DISPLAY_NAME", default="")
SUPPORT_EMAIL = env("SUPPORT_EMAIL", default="")


# ---------------------------------------------------------------------------
# Logging
#
# Formatters deliberately log identifiers (message ids, error codes) but never
# credentials. See core.logging_filters.RedactSecretsFilter.
# ---------------------------------------------------------------------------

LOG_LEVEL = env("LOG_LEVEL", default="INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "redact_secrets": {
            "()": "core.logging_filters.RedactSecretsFilter",
        },
    },
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["redact_secrets"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "django.db.backends": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "whatsapp": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "campaigns": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "contacts": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "accounts": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}
