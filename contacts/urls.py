"""HTML routes for contacts, groups and CSV import."""

from django.urls import path

from contacts import views

app_name = "contacts"

urlpatterns = [
    # Contacts
    path("", views.ContactListView.as_view(), name="list"),
    path("new/", views.ContactCreateView.as_view(), name="create"),
    path("<uuid:pk>/", views.ContactDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", views.ContactUpdateView.as_view(), name="update"),
    path("<uuid:pk>/delete/", views.ContactDeleteView.as_view(), name="delete"),
    path(
        "<uuid:pk>/consent/<str:action>/",
        views.ContactConsentView.as_view(),
        name="consent",
    ),
    # CSV import
    path("import/", views.ContactImportView.as_view(), name="import"),
    path("import/history/", views.ContactImportListView.as_view(), name="import-list"),
    path("import/sample.csv", views.SampleCsvView.as_view(), name="import-sample"),
    path("import/<uuid:pk>/", views.ContactImportDetailView.as_view(), name="import-detail"),
    # Groups
    path("groups/", views.GroupListView.as_view(), name="group-list"),
    path("groups/new/", views.GroupCreateView.as_view(), name="group-create"),
    path("groups/<uuid:pk>/", views.GroupDetailView.as_view(), name="group-detail"),
    path("groups/<uuid:pk>/edit/", views.GroupUpdateView.as_view(), name="group-update"),
    path("groups/<uuid:pk>/delete/", views.GroupDeleteView.as_view(), name="group-delete"),
    path("groups/<uuid:pk>/members/", views.GroupMembersView.as_view(), name="group-members"),
]
