"""Report whether the sending pipeline is actually able to send right now."""

from django.core.management.base import BaseCommand

from whatsapp.health import pipeline_status


class Command(BaseCommand):
    help = "Show the WhatsApp provider, queue broker and rate limiter status."

    def handle(self, *args, **options) -> None:
        status = pipeline_status()

        def line(label: str, value: str, ok: bool | None = None) -> None:
            style = self.style.SUCCESS if ok else (self.style.ERROR if ok is False else str)
            self.stdout.write(f"  {label:<24} {style(value)}")

        self.stdout.write(self.style.MIGRATE_HEADING("Sending pipeline"))
        line("Provider", status["provider"], None)
        line(
            "Delivery",
            "simulated (nothing is sent)" if status["is_simulated"] else "live",
            None,
        )
        line(
            "Dispatcher",
            "registered" if status["dispatcher_registered"] else "NOT registered",
            status["dispatcher_registered"],
        )
        line(
            "Queue broker",
            "reachable" if status["broker_reachable"] else "unreachable",
            status["broker_reachable"],
        )
        if not status["broker_reachable"]:
            self.stdout.write(f"  {'':<24} {status['broker_detail']}")
        line("Rate limiter", status["rate_limiter"], None)
        line("Send ceiling", f"{status['send_rate_per_second']}/second", None)

        self.stdout.write("")
        if status["can_send"]:
            self.stdout.write(self.style.SUCCESS("Campaigns can be launched."))
        else:
            self.stdout.write(
                self.style.ERROR(
                    "Campaigns cannot be launched until the problems above are resolved."
                )
            )
