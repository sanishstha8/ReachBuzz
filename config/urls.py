"""
Root URL configuration.

HTML pages live at the root; the REST API is namespaced under /api/. Apps are
added to both lists as later phases land them.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from billing.webhooks import PaymentWebhookView
from whatsapp.webhooks import MetaWebhookView

urlpatterns = [
    path("admin/", admin.site.urls),
    # --- HTML pages --------------------------------------------------------
    # The public landing page owns the root; the operator dashboard sits one
    # level in. Everything links by name, so moving it breaks no reverse().
    path("", include("pages.urls", namespace="pages")),
    path("dashboard/", include("dashboard.urls", namespace="dashboard")),
    path("accounts/", include("accounts.urls", namespace="accounts")),
    path("contacts/", include("contacts.urls", namespace="contacts")),
    path("campaigns/", include("campaigns.urls", namespace="campaigns")),
    path("templates/", include("whatsapp.urls", namespace="whatsapp")),
    path("billing/", include("billing.urls", namespace="billing")),
    path("organization/", include("organizations.urls", namespace="organizations")),
    # Staff only, and the only place in the application that reads across the
    # tenant boundary on purpose. See backoffice/access.py.
    path("backoffice/", include("backoffice.urls", namespace="backoffice")),
    # --- Inbound webhooks --------------------------------------------------
    # Unauthenticated and CSRF-exempt by necessity: Meta calls it. The HMAC
    # signature over the raw body is what authenticates the request.
    path("api/whatsapp/webhook/", MetaWebhookView.as_view(), name="whatsapp-webhook"),
    # Same reasoning, higher stakes: a forged delivery report is a wrong number
    # on a dashboard, a forged payment notification is money.
    path("api/billing/webhook/", PaymentWebhookView.as_view(), name="payment-webhook"),
    # --- REST API, version 1 -------------------------------------------------
    # These are the canonical routes and the ones `reverse()` produces, because
    # they carry the original namespaces. A client reading a URL out of this
    # application gets the versioned form.
    path("api/v1/auth/", include("accounts.api_urls", namespace="accounts-api")),
    path("api/v1/", include("contacts.api_urls", namespace="contacts-api")),
    path("api/v1/", include("whatsapp.api_urls", namespace="whatsapp-api")),
    path("api/v1/", include("campaigns.api_urls", namespace="campaigns-api")),
    path("api/v1/", include("messaging.api_urls", namespace="messaging-api")),
    path("api/v1/", include("dashboard.api_urls", namespace="dashboard-api")),
    #
    # --- The same API, at the addresses it has always had ---------------------
    # Every route above is also served unversioned, because that is where the
    # existing integrations are pointing and a version scheme is not a reason to
    # break them.
    #
    # **There is no removal date, and none is invented here.** Announcing a
    # sunset this project has not actually decided on would be the same kind of
    # fabrication as printing a price nobody has agreed — and an unmet
    # deprecation promise teaches clients to ignore the next one. If these are
    # ever retired it will be a deliberate decision with notice behind it.
    #
    # Registered under distinct instance namespaces so `reverse()` stays
    # unambiguous: two includes sharing a namespace would leave which one wins
    # up to registration order, and it must always be v1.
    path(
        "api/auth/",
        include(("accounts.api_urls", "accounts-api"), namespace="accounts-api-unversioned"),
    ),
    path(
        "api/",
        include(("contacts.api_urls", "contacts-api"), namespace="contacts-api-unversioned"),
    ),
    path(
        "api/",
        include(("whatsapp.api_urls", "whatsapp-api"), namespace="whatsapp-api-unversioned"),
    ),
    path(
        "api/",
        include(("campaigns.api_urls", "campaigns-api"), namespace="campaigns-api-unversioned"),
    ),
    path(
        "api/",
        include(("messaging.api_urls", "messaging-api"), namespace="messaging-api-unversioned"),
    ),
    path(
        "api/",
        include(("dashboard.api_urls", "dashboard-api"), namespace="dashboard-api-unversioned"),
    ),
    # --- API documentation -------------------------------------------------
    path("api/schema/", SpectacularAPIView.as_view(), name="api-schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="api-schema"),
        name="api-docs",
    ),
    path(
        "api/redoc/",
        SpectacularRedocView.as_view(url_name="api-schema"),
        name="api-redoc",
    ),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

    if "debug_toolbar" in settings.INSTALLED_APPS:
        urlpatterns += [path("__debug__/", include("debug_toolbar.urls"))]
