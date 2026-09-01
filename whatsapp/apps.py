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
        """
        from campaigns import dispatch
        from whatsapp import tasks

        dispatch.register_dispatcher(tasks.queue_campaign)
