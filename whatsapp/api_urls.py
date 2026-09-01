"""REST routes for templates (mounted at /api/)."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from whatsapp import api_views

app_name = "whatsapp-api"

router = DefaultRouter()
router.register("templates", api_views.MessageTemplateViewSet, basename="template")

urlpatterns = [path("", include(router.urls))]
