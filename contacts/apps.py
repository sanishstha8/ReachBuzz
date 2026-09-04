from django.apps import AppConfig


class ContactsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "contacts"
    verbose_name = "Contacts"

    def ready(self) -> None:
        """
        Register the per-channel consent model, which lives outside models.py.

        Consent is the most consequential thing in this application and now has
        two storage shapes for historical reasons, so it is worth a file of its
        own that can explain itself. Django only discovers models in modules it
        imports, and it imports ``models``.
        """
        from contacts import consent  # noqa: F401
