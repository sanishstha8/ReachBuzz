"""
HTML routes for authentication.

Password reset uses Django's own four-step views rather than a hand-rolled
flow: they already get the token generation, timing and one-time semantics
right, and this project only needs to supply the templates.
"""

from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from accounts import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("register/", views.RegisterView.as_view(), name="register"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    # --- Confirming an address ---------------------------------------------
    path(
        "verify/<uidb64>/<token>/",
        views.VerifyEmailView.as_view(),
        name="verify-email",
    ),
    path("verify/resend/", views.ResendVerificationView.as_view(), name="resend-verification"),
    # --- Forgotten passwords -----------------------------------------------
    path(
        "password/reset/",
        views.ThrottledPasswordResetView.as_view(
            template_name="accounts/password_reset.html",
            email_template_name="accounts/email/password_reset.txt",
            subject_template_name="accounts/email/password_reset_subject.txt",
            success_url=reverse_lazy("accounts:password-reset-sent"),
        ),
        name="password-reset",
    ),
    path(
        "password/reset/sent/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="accounts/password_reset_sent.html"
        ),
        name="password-reset-sent",
    ),
    path(
        "password/reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="accounts/password_reset_confirm.html",
            success_url=reverse_lazy("accounts:password-reset-complete"),
        ),
        name="password-reset-confirm",
    ),
    path(
        "password/reset/done/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="accounts/password_reset_complete.html"
        ),
        name="password-reset-complete",
    ),
]
