"""REST routes for message records (mounted at /api/)."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from messaging import api_views

app_name = "messaging-api"

router = DefaultRouter()
router.register("messages", api_views.MessageViewSet, basename="message")

urlpatterns = [
    path("messages/stats/", api_views.MessageStatsAPIView.as_view(), name="message-stats"),
    path("", include(router.urls)),
]
