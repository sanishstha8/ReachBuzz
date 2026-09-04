"""
The public landing page.

The only unauthenticated HTML in the project, and the only place that talks
about the product rather than operating it. Two rules follow from that.

**Nothing here may claim a capability the system does not have.** The page is
marketing, but it is marketing for software whose whole design is about not
overstating things — a page promising a flow that does not exist is the same
failure as a dashboard showing a fabricated zero. Every feature listed below is
one you can point at in the codebase. The call to action read "Request access"
until self-service signup was actually built, and only then became "Get
started".

**Prices are data, not prose — and now they are rows.** Tiers come from
``billing.models.Plan``, so what this page advertises is what ``billing.usage``
enforces. They were a hard-coded tuple in this file until Stage 3, which meant
the page could promise "Up to 1,000 contacts" while nothing counted them. A plan
carries ``price = None`` until someone sets a real figure, and the template
renders "Pricing on request" for a tier without one. Inventing a number to fill
the layout is the one thing this file will not do.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.views.generic import TemplateView

from billing.models import Plan


@dataclass(frozen=True)
class Feature:
    icon: str
    title: str
    body: str


@dataclass(frozen=True)
class Step:
    icon: str
    title: str
    body: str


@dataclass(frozen=True)
class Question:
    question: str
    answer: str


# Each of these is a thing the application actually does. Where a claim needed
# a qualifier to stay true, the qualifier is in the copy rather than omitted.
FEATURES: tuple[Feature, ...] = (
    Feature(
        "people",
        "Contact management",
        "Add contacts by hand or import a CSV. Numbers are normalised to E.164, "
        "duplicates are caught on the way in, and consent is recorded with a source "
        "and a timestamp.",
    ),
    Feature(
        "send",
        "Bulk campaigns",
        "Build an audience from groups, preview exactly what each recipient will get, "
        "then launch. Sending runs in a background queue, so a thousand recipients "
        "never blocks a page load.",
    ),
    Feature(
        "file-earmark-text",
        "Approved templates",
        "Your WhatsApp templates are mirrored from Meta with their real approval "
        "status. Variables are filled per recipient and shown to you before anything "
        "is sent.",
    ),
    Feature(
        "bar-chart",
        "Delivery reporting",
        "Delivery, read and failure rates per campaign and across any date range, "
        "with the provider's own error codes grouped so one bad number is easy to "
        "tell from a systemic problem.",
    ),
    Feature(
        "clock-history",
        "Scheduling and retries",
        "Schedule a campaign for later, pause it mid-send, or resume it. Transient "
        "failures retry with backoff; permanent ones stop rather than burning your "
        "quota.",
    ),
    Feature(
        "shield-check",
        "Consent, enforced",
        "A campaign can only reach contacts who opted in and are still active. That "
        "rule lives in one function with no override, and every consent change is "
        "written to an append-only audit log.",
    ),
)

STEPS: tuple[Step, ...] = (
    Step("people", "Add your contacts", "Import a CSV or add them one at a time, recording how each person consented."),
    Step("file-earmark-text", "Pick a template", "Choose one of your Meta-approved templates and map its variables to contact fields."),
    Step("send", "Preview, then launch", "See the exact message and the real recipient count before you commit to sending."),
    Step("bar-chart", "Watch it land", "Delivery and read receipts stream back from WhatsApp and update the campaign live."),
)

FAQ: tuple[Question, ...] = (
    Question(
        "Do I need my own WhatsApp Business account?",
        "Yes. Messages are sent through the official Meta WhatsApp Business Platform "
        "Cloud API using your own WhatsApp Business Account and phone number. You "
        "connect it once with credentials from your Meta app.",
    ),
    Question(
        "Can I message anyone whose number I have?",
        "No, and there is no setting that changes this. A campaign resolves its "
        "audience through a single rule — the contact opted in and is still active — "
        "and no part of the system can bypass it. A CSV import marks somebody opted "
        "in only when a consent column says so explicitly.",
    ),
    Question(
        "What happens when someone replies STOP?",
        "They are opted out automatically, with the reason recorded as an inbound "
        "message and an entry written to the audit log. The match is against the "
        "whole message, so a reply that merely contains the word does not opt anyone "
        "out by accident. Opting back in is always a deliberate act.",
    ),
    Question(
        "Who approves the message templates?",
        "Meta does, in WhatsApp Manager. This platform mirrors what Meta reports and "
        "cannot mark a template approved or submit one for review. A template Meta "
        "has not approved is refused at launch rather than failing mid-send.",
    ),
    Question(
        "How fast does it send, and what if a message fails?",
        "One background job per recipient, throttled by a rate ceiling you set to stay "
        "inside your WhatsApp Business Account's limits. Transient failures retry with "
        "exponential backoff; permanent ones are recorded with the provider's error "
        "code and not retried, because retrying them wastes quota and depresses your "
        "delivery rate.",
    ),
    Question(
        "Is there an API?",
        "Yes — every resource has REST endpoints with an OpenAPI schema and browsable "
        "documentation. Message state is read-only over the API, because it belongs to "
        "the sending worker and the provider's webhooks.",
    ),
)


class LandingView(TemplateView):
    """Public marketing page. Deliberately has no login requirement."""

    template_name = "pages/landing.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["features"] = FEATURES
        context["steps"] = STEPS
        context["faq"] = FAQ
        context["support_email"] = getattr(settings, "SUPPORT_EMAIL", "")

        # Read from the plan catalogue rather than from a tuple in this file.
        # The limits advertised here are the limits actually enforced, because
        # they are now the same rows - a promise on this page and a ceiling in
        # billing.usage can no longer drift apart, which they could while the
        # copy said "Up to 1,000 contacts" and nothing checked.
        tiers = list(Plan.objects.public())
        context["tiers"] = tiers
        context["any_price_set"] = any(tier.has_price for tier in tiers)
        return context
