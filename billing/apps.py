from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "billing"
    verbose_name = "Billing"

    def ready(self) -> None:
        """
        Register the models that live outside ``models.py``.

        Entitlement (what a customer may do) and money (what they owe) have
        different rules and are worth reading separately, so the invoice models
        live in ``billing.invoicing``. Django only discovers models in modules
        it imports, and it imports ``models`` - hence this.
        """
        from billing import invoicing  # noqa: F401
