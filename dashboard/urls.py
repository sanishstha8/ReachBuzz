"""HTML routes for the dashboard and reports."""

from django.urls import path

from dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("reports/", views.ReportsView.as_view(), name="reports"),
    path("reports/download/<slug:report>.csv", views.ReportDownloadView.as_view(), name="report-download"),
    path(
        "reports/campaigns/<uuid:pk>/recipients.csv",
        views.CampaignRecipientsReportView.as_view(),
        name="campaign-recipients-report",
    ),
]
