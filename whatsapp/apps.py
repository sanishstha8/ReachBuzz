from django.apps import AppConfig


class WhatsappConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "whatsapp"
    verbose_name = "WhatsApp"

    def ready(self) -> None:
        """
        Register the Celery sender with the campaigns app.

        This is the seam Phase 4 left open: campaign services know only that
        *a* dispatcher exists, never that it is Celery, which keeps them
        testable without a broker.

        Also registers the models that live outside ``models.py``. Django only
        discovers models in modules it imports, and per-organization credentials
        are enough of their own subject — encryption, provider resolution, the
        webhook routing that depends on them — to be worth reading separately.
        """
        from campaigns import dispatch
        from whatsapp import accounts, tasks  # noqa: F401

        dispatch.register_dispatcher(tasks.queue_campaign)
