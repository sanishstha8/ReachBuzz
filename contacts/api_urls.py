"""REST routes for contacts (mounted at /api/)."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from contacts import api_views

app_name = "contacts-api"

router = DefaultRouter()
router.register("contacts", api_views.ContactViewSet, basename="contact")
router.register("contact-groups", api_views.ContactGroupViewSet, basename="contactgroup")
router.register("contact-imports", api_views.ContactImportViewSet, basename="contactimport")

urlpatterns = [
    path(
        "contacts/import/",
        api_views.ContactImportCreateAPIView.as_view(),
        name="contact-import-create",
    ),
    path("contacts/stats/", api_views.ContactStatsAPIView.as_view(), name="contact-stats"),
    path("", include(router.urls)),
]
