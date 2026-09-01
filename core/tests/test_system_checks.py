"""
Django's own system checks, run as part of the suite.

Admin misconfiguration (a `list_display` entry that no longer exists, say) only
surfaces at startup otherwise. That is too late: the test suite should fail
before a deploy does.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError


class TestSystemChecks:
    def test_project_passes_django_system_checks(self) -> None:
        try:
            call_command("check", verbosity=0)
        except SystemCheckError as exc:  # pragma: no cover - failure path
            pytest.fail(f"Django system checks failed:\n{exc}")

    def test_no_missing_migrations(self, db) -> None:
        """A model change without a migration would break the next deploy."""
        try:
            call_command("makemigrations", "--check", "--dry-run", verbosity=0)
        except SystemExit as exc:  # pragma: no cover - failure path
            pytest.fail(f"Models have changes with no matching migration (exit {exc.code}).")
