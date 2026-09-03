"""
The public landing page.

The only unauthenticated HTML in the project, and the only place that talks
about the product rather than operating it. Two rules follow from that.

**Nothing here may claim a capability the system does not have.** The page is
marketing, but it is marketing for software whose whole design is about not
overstating things — a landing page promising a self-service signup that does
not exist would be the same failure as a dashboard showing a fabricated zero.
Every feature listed below is one you can point at in the codebase.

**Prices are data, not prose.** ``PRICING_TIERS`` carries ``price = None``
until someone sets a real figure, and the template renders "Pricing on
request" for a tier without one. Inventing a number to fill the layout is the
one thing this file will not do.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.views.generic import TemplateView


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
class Tier:
    name: str
    price: str | None
    period: str
    summary: str
    features: tuple[str, ...]
    featured: bool = False

    @property
    def has_price(self) -> bool:
        return bool(self.price)


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

# price = None renders as "Pricing on request". Set a real figure when there is
# one; do not invent a placeholder that looks like a decision has been made.
PRICING_TIERS: tuple[Tier, ...] = (
    Tier(
        name="Starter",
        price=None,
        period="per month",
        summary="For a single team sending occasional campaigns.",
        features=(
            "Up to 1,000 contacts",
            "CSV import and contact groups",
            "Approved template messaging",
            "Delivery and read reporting",
            "Email support",
        ),
    ),
    Tier(
        name="Business",
        price=None,
        period="per month",
        summary="For regular campaigns to a growing audience.",
        features=(
            "Up to 10,000 contacts",
            "Everything in Starter",
            "Scheduled campaigns",
            "CSV exports and the reporting API",
            "Role-based access for your team",
        ),
        featured=True,
    ),
    Tier(
        name="Self-hosted",
        price=None,
        period="",
        summary="Run it on your own infrastructure, against your own WABA.",
        features=(
            "No contact limit",
            "Your database, your Redis, your logs",
            "Full REST API and OpenAPI schema",
            "Connect your own Meta Business account",
            "Deployment guidance",
        ),
    ),
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
        context["tiers"] = PRICING_TIERS
        context["faq"] = FAQ
        context["support_email"] = getattr(settings, "SUPPORT_EMAIL", "")
        context["any_price_set"] = any(tier.has_price for tier in PRICING_TIERS)
        return context
