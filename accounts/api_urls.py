"""REST routes for authentication (mounted at /api/auth/)."""

from django.urls import path

from accounts import api_views

app_name = "accounts-api"

urlpatterns = [
    path("login/", api_views.LoginAPIView.as_view(), name="login"),
    path("logout/", api_views.LogoutAPIView.as_view(), name="logout"),
    path("me/", api_views.CurrentUserAPIView.as_view(), name="me"),
]
