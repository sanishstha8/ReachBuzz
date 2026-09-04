"""Backoffice routes. Staff only; see backoffice.access for the gate."""

from django.urls import path

from backoffice import views

app_name = "backoffice"

urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
    path("organizations/", views.OrganizationListView.as_view(), name="organizations"),
    path(
        "organizations/<uuid:pk>/",
        views.OrganizationDetailView.as_view(),
        name="organization-detail",
    ),
    path("health/", views.HealthView.as_view(), name="health"),
]
