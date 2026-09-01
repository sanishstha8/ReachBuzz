"""HTML routes for campaigns."""

from django.urls import path

from campaigns import views

app_name = "campaigns"

urlpatterns = [
    path("", views.CampaignListView.as_view(), name="list"),
    path("new/", views.CampaignCreateView.as_view(), name="create"),
    path("<uuid:pk>/", views.CampaignDetailView.as_view(), name="detail"),
    path("<uuid:pk>/recipients/", views.CampaignMessagesView.as_view(), name="messages"),
    path("<uuid:pk>/delete/", views.CampaignDeleteView.as_view(), name="delete"),
    path("<uuid:pk>/action/<str:action>/", views.CampaignActionView.as_view(), name="action"),
    # Wizard
    path("<uuid:pk>/edit/", views.CampaignDetailsUpdateView.as_view(), name="wizard-details"),
    path("<uuid:pk>/audience/", views.CampaignAudienceView.as_view(), name="wizard-audience"),
    path("<uuid:pk>/message/", views.CampaignMessageView.as_view(), name="wizard-message"),
    path("<uuid:pk>/preview/", views.CampaignPreviewView.as_view(), name="wizard-preview"),
    path("<uuid:pk>/confirm/", views.CampaignConfirmView.as_view(), name="wizard-confirm"),
]
