"""HTML routes for message templates."""

from django.urls import path

from whatsapp import views

app_name = "whatsapp"

urlpatterns = [
    path("", views.TemplateListView.as_view(), name="template-list"),
    path("new/", views.LocalTemplateCreateView.as_view(), name="template-create"),
    path("<uuid:pk>/", views.TemplateDetailView.as_view(), name="template-detail"),
]
