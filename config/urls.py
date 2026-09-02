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

from whatsapp.webhooks import MetaWebhookView

urlpatterns = [
    path("admin/", admin.site.urls),
    # --- HTML pages --------------------------------------------------------
    path("", include("dashboard.urls", namespace="dashboard")),
    path("accounts/", include("accounts.urls", namespace="accounts")),
    path("contacts/", include("contacts.urls", namespace="contacts")),
    path("campaigns/", include("campaigns.urls", namespace="campaigns")),
    path("templates/", include("whatsapp.urls", namespace="whatsapp")),
    # --- Inbound webhooks --------------------------------------------------
    # Unauthenticated and CSRF-exempt by necessity: Meta calls it. The HMAC
    # signature over the raw body is what authenticates the request.
    path("api/whatsapp/webhook/", MetaWebhookView.as_view(), name="whatsapp-webhook"),
    # --- REST API ----------------------------------------------------------
    path("api/auth/", include("accounts.api_urls", namespace="accounts-api")),
    path("api/", include("contacts.api_urls", namespace="contacts-api")),
    path("api/", include("whatsapp.api_urls", namespace="whatsapp-api")),
    path("api/", include("campaigns.api_urls", namespace="campaigns-api")),
    path("api/", include("messaging.api_urls", namespace="messaging-api")),
    path("api/", include("dashboard.api_urls", namespace="dashboard-api")),
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
