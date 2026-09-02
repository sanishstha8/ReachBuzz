"""REST routes for reporting and monitoring (mounted at /api/)."""

from django.urls import path

from dashboard import api_views

app_name = "dashboard-api"

urlpatterns = [
    path("reports/overview/", api_views.ReportOverviewAPIView.as_view(), name="report-overview"),
    path("reports/activity/", api_views.ReportActivityAPIView.as_view(), name="report-activity"),
    path("reports/campaigns/", api_views.ReportCampaignsAPIView.as_view(), name="report-campaigns"),
    path("reports/failures/", api_views.ReportFailuresAPIView.as_view(), name="report-failures"),
    path("reports/consent/", api_views.ConsentSummaryAPIView.as_view(), name="report-consent"),
    path(
        "monitor/active-campaigns/",
        api_views.ActiveCampaignsAPIView.as_view(),
        name="active-campaigns",
    ),
]
