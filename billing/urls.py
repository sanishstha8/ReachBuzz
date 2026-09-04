"""Customer-facing billing routes."""

from django.urls import path

from billing import views

app_name = "billing"

urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
    path("plans/", views.PlanListView.as_view(), name="plans"),
    # POST only: changing what somebody pays is not a GET, and a plan-change
    # link would be followed by every prefetcher and link scanner that sees it.
    path("plans/<slug:slug>/choose/", views.ChangePlanView.as_view(), name="change-plan"),
    path("cancel/", views.CancelView.as_view(), name="cancel"),
    path("resume/", views.ResumeView.as_view(), name="resume"),
    path("invoices/", views.InvoiceListView.as_view(), name="invoices"),
    path("invoices/<uuid:pk>/", views.InvoiceDetailView.as_view(), name="invoice-detail"),
]
