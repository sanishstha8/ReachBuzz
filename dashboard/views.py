"""
Dashboard.

Tiles show real numbers for the modules that have shipped and an em dash for
those that have not — a fabricated zero reads as "nothing has happened", which
is a claim the system cannot make about a module that does not exist yet.

Phase 6 adds charts and downloadable reports on top of these figures.
"""

from __future__ import annotations

from django.apps import apps
from django.conf import settings
from django.db.models import Count, Q
from django.views.generic import TemplateView

from core.mixins import ActiveUserRequiredMixin, PageTitleMixin


class HomeView(ActiveUserRequiredMixin, PageTitleMixin, TemplateView):
    template_name = "dashboard/home.html"
    page_title = "Dashboard"
    active_nav = "dashboard"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["modules"] = {
            "contacts": apps.is_installed("contacts"),
            "campaigns": apps.is_installed("campaigns"),
            "messaging": apps.is_installed("messaging"),
            "whatsapp": apps.is_installed("whatsapp"),
        }
        context["contact_stats"] = self._contact_stats()
        context["campaign_stats"] = self._campaign_stats()
        context["message_stats"] = self._message_stats()
        context["recent_campaigns"] = self._recent_campaigns()
        context["system_status"] = self._system_status()
        return context

    # -- Statistics ---------------------------------------------------------
    #
    # Each helper returns None while its app is not installed, which is what
    # makes the template render an em dash instead of a misleading zero.

    @staticmethod
    def _contact_stats() -> dict[str, int] | None:
        if not apps.is_installed("contacts"):
            return None

        from contacts.models import Contact, ContactGroup, ContactStatus

        aggregates = Contact.objects.aggregate(
            total_count=Count("id"),
            opted_in_count=Count("id", filter=Q(opted_in=True)),
            eligible_count=Count("id", filter=Q(opted_in=True, status=ContactStatus.ACTIVE)),
        )
        return {
            "total": aggregates["total_count"],
            "opted_in": aggregates["opted_in_count"],
            "eligible": aggregates["eligible_count"],
            "groups": ContactGroup.objects.count(),
        }

    @staticmethod
    def _campaign_stats() -> dict[str, int] | None:
        if not apps.is_installed("campaigns"):
            return None

        from campaigns.models import Campaign, CampaignStatus

        aggregates = Campaign.objects.aggregate(
            total_count=Count("id"),
            draft_count=Count("id", filter=Q(status=CampaignStatus.DRAFT)),
            processing_count=Count("id", filter=Q(status=CampaignStatus.PROCESSING)),
            completed_count=Count("id", filter=Q(status=CampaignStatus.COMPLETED)),
        )
        return {
            "total": aggregates["total_count"],
            "draft": aggregates["draft_count"],
            "processing": aggregates["processing_count"],
            "completed": aggregates["completed_count"],
        }

    @staticmethod
    def _message_stats() -> dict[str, int] | None:
        if not apps.is_installed("messaging"):
            return None

        from messaging.services import global_stats

        return global_stats()

    @staticmethod
    def _recent_campaigns():
        if not apps.is_installed("campaigns"):
            return []

        from campaigns.models import Campaign
        from messaging.models import MessageStatus

        return (
            Campaign.objects.select_related("template")
            .annotate(
                message_count=Count("messages", distinct=True),
                delivered_count=Count(
                    "messages",
                    filter=Q(messages__status__in=[MessageStatus.DELIVERED, MessageStatus.READ]),
                    distinct=True,
                ),
                failed_count=Count(
                    "messages", filter=Q(messages__status=MessageStatus.FAILED), distinct=True
                ),
            )
            .order_by("-created_at")[:8]
        )

    @staticmethod
    def _system_status() -> dict[str, object]:
        """
        Whether a campaign could actually be sent right now.

        "A dispatcher is registered" is not that answer — the Celery sender
        registers at startup whether or not Redis is running — so this probes
        the broker (cached briefly) rather than reporting a proxy for it.
        """
        status = {
            "provider": settings.WHATSAPP_PROVIDER,
            "celery_broker_configured": bool(settings.CELERY_BROKER_URL),
            "send_rate_per_second": settings.WHATSAPP_SEND_RATE_PER_SECOND,
            "default_country": settings.DEFAULT_COUNTRY_CODE,
            "dispatcher_registered": False,
            "broker_reachable": False,
            "broker_detail": "",
            "can_send": False,
        }

        if apps.is_installed("whatsapp"):
            from whatsapp.health import pipeline_status

            status.update(pipeline_status())

        return status
