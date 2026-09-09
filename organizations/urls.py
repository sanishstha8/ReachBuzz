"""HTML routes for team membership."""

from django.urls import path

from organizations import views

app_name = "organizations"

urlpatterns = [
    path("members/", views.MemberListView.as_view(), name="members"),
    path("members/invite/", views.InviteMemberView.as_view(), name="invite-member"),
    path(
        "members/<uuid:pk>/remove/", views.RemoveMemberView.as_view(), name="remove-member"
    ),
    path(
        "invitations/<uuid:pk>/resend/",
        views.ResendInvitationView.as_view(),
        name="resend-invitation",
    ),
    path(
        "invitations/<uuid:pk>/revoke/",
        views.RevokeInvitationView.as_view(),
        name="revoke-invitation",
    ),
    # Public: reached from the emailed link, by someone who may not be signed in.
    path(
        "invitations/<str:token>/accept/",
        views.AcceptInvitationView.as_view(),
        name="invitation-accept",
    ),
]
