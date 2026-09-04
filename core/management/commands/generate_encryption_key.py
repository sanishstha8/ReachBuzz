"""Generate a key for FIELD_ENCRYPTION_KEY."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from core.encryption import generate_key


class Command(BaseCommand):
    help = "Generate a Fernet key for FIELD_ENCRYPTION_KEY."

    def handle(self, *args, **options) -> None:
        self.stdout.write(generate_key())
        self.stderr.write(
            self.style.WARNING(
                "\nPut this in FIELD_ENCRYPTION_KEY and keep it with your other secrets.\n"
                "Changing it makes every stored provider credential unreadable; there is\n"
                "no recovery except re-entering them."
            )
        )
