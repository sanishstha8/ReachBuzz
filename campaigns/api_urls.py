"""REST routes for campaigns (mounted at /api/)."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from campaigns import api_views

app_name = "campaigns-api"

router = DefaultRouter()
router.register("campaigns", api_views.CampaignViewSet, basename="campaign")

urlpatterns = [path("", include(router.urls))]
