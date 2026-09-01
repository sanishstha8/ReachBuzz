"""
Custom user model.

Defined up front, before the first migration, because swapping ``AUTH_USER_MODEL``
after tables exist is expensive. Sign-in uses email rather than a separate
username, and a coarse role decides what each account may do.
"""

from __future__ import annotations

import uuid

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class UserRole(models.TextChoices):
    ADMINISTRATOR = "admin", _("Administrator")
    OPERATOR = "operator", _("Operator")
    VIEWER = "viewer", _("Viewer")


class UserManager(BaseUserManager):
    """Manager for a user model that authenticates by email."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        extra_fields.setdefault("role", UserRole.OPERATOR)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email: str, password: str | None = None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", UserRole.ADMINISTRATOR)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    """Authorized operator of the messaging platform."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Authentication is by email; the inherited username field is not used.
    username = None
    email = models.EmailField(_("email address"), unique=True)

    role = models.CharField(
        max_length=16,
        choices=UserRole.choices,
        default=UserRole.OPERATOR,
        help_text=_("Determines which actions this user may perform."),
    )
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    class Meta:
        ordering = ["email"]
        verbose_name = _("user")
        verbose_name_plural = _("users")

    def __str__(self) -> str:
        return self.email

    # -- Display ------------------------------------------------------------

    @property
    def display_name(self) -> str:
        full_name = self.get_full_name().strip()
        return full_name or self.email

    @property
    def initials(self) -> str:
        parts = [p for p in (self.first_name, self.last_name) if p]
        if parts:
            return "".join(p[0] for p in parts).upper()[:2]
        return self.email[:2].upper()

    # -- Capabilities -------------------------------------------------------
    #
    # Views and permission classes ask for a capability, never for a role, so
    # the policy below is the single place the matrix is defined.

    @property
    def is_administrator(self) -> bool:
        return self.is_superuser or self.role == UserRole.ADMINISTRATOR

    @property
    def can_manage_contacts(self) -> bool:
        return self.is_administrator or self.role == UserRole.OPERATOR

    @property
    def can_manage_campaigns(self) -> bool:
        return self.is_administrator or self.role == UserRole.OPERATOR

    @property
    def can_launch_campaigns(self) -> bool:
        """Sending is irreversible, so it is kept as its own capability."""
        return self.is_administrator or self.role == UserRole.OPERATOR

    # -- Bookkeeping --------------------------------------------------------

    def record_login(self, ip_address: str | None) -> None:
        self.last_login_ip = ip_address
        self.last_login = timezone.now()
        self.save(update_fields=["last_login_ip", "last_login"])
