"""Public routes. No authentication anywhere in this module."""

from django.urls import path

from pages import views

app_name = "pages"

urlpatterns = [
    path("", views.LandingView.as_view(), name="landing"),
]
