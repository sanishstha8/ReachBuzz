"""
The SMS provider contract.

Third provider seam in this project, and the first one that revealed the shape
of the second was wrong. ``WhatsAppProvider`` requires ``send_template(name,
language, header_variables)`` and ``fetch_templates()`` — an interface built
around Meta's approval registry, which SMS simply does not have. There is no
upstream catalogue to sync, no language variant to select and nothing to get
approved. Making SMS implement that interface would have meant three methods
raising ``NotImplementedError`` and a fourth pretending a template name was
something a gateway understood.

So SMS gets its own contract, deliberately smaller: **send some text to a
number, and tell me what happened.** That is the whole of it. What the two
share is not an interface but a *shape* — a frozen result object that says
whether it worked and whether retrying is worth it — and sharing the shape is
what lets one Celery task drive either.

Two things this interface encodes that a naive SMS wrapper would not:

**Segments, not characters.** A gateway charges per 160-character GSM-7 segment,
or per 70 characters once any single character forces UCS-2. One emoji in a
message turns a one-segment send into a three-segment one and triples its cost,
silently. :func:`segment_count` is here so nothing has to rediscover that.

**Retryable is a decision, not a status code.** Same reasoning as the WhatsApp
provider: the Celery task needs to know whether to try again, and a gateway that
rejected a number as invalid will reject it identically forever.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

#: Characters representable in the GSM 03.38 alphabet. Anything outside it
#: forces the whole message into UCS-2, which more than halves the segment size.
GSM7_CHARS = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
#: These occupy two GSM-7 positions each rather than one.
GSM7_EXTENDED = set("^{}\\[~]|€")

SEGMENT_GSM7 = 160
SEGMENT_GSM7_MULTIPART = 153  # a concatenation header eats seven characters
SEGMENT_UCS2 = 70
SEGMENT_UCS2_MULTIPART = 67


def is_gsm7(text: str) -> bool:
    return all(char in GSM7_CHARS or char in GSM7_EXTENDED for char in text)


def segment_count(text: str) -> int:
    """
    How many segments a gateway will bill for this body.

    Worth getting right rather than approximating: a customer who writes 155
    characters and adds one emoji goes from one segment to three, and finding
    that out on an invoice is a bad way to find it out.
    """
    if not text:
        return 0

    if is_gsm7(text):
        length = sum(2 if char in GSM7_EXTENDED else 1 for char in text)
        single, multi = SEGMENT_GSM7, SEGMENT_GSM7_MULTIPART
    else:
        length = len(text)
        single, multi = SEGMENT_UCS2, SEGMENT_UCS2_MULTIPART

    if length <= single:
        return 1
    return -(-length // multi)  # ceiling division


@dataclass(frozen=True)
class SmsResult:
    """
    The outcome of one send attempt.

    Same shape as ``whatsapp.services.base.SendResult`` on purpose. The two
    providers have different interfaces and identical *results*, which is what
    lets the sending task treat a failure the same way whichever channel
    produced it.
    """

    success: bool
    provider_message_id: str = ""
    segments: int = 0
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False
    retry_after: int | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def ok(cls, provider_message_id: str, segments: int, raw: dict | None = None) -> SmsResult:
        return cls(
            success=True,
            provider_message_id=provider_message_id,
            segments=segments,
            raw=raw or {},
        )

    @classmethod
    def failure(
        cls,
        error_code: str,
        error_message: str,
        *,
        retryable: bool = False,
        retry_after: int | None = None,
        raw: dict | None = None,
    ) -> SmsResult:
        return cls(
            success=False,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
            retry_after=retry_after,
            raw=raw or {},
        )


@dataclass(frozen=True)
class SmsStatus:
    """A delivery receipt for a message we sent."""

    provider_message_id: str
    status: str
    timestamp: datetime | None = None
    error_code: str = ""
    error_message: str = ""
    raw: dict = field(default_factory=dict)


class SmsProvider(ABC):
    """Base class every SMS gateway implements."""

    #: Short identifier matching the SMS_PROVIDER setting.
    name: str = "base"

    #: True when nothing actually leaves the machine.
    is_simulated: bool = False

    def check_configuration(self) -> None:
        """
        Raise :class:`~core.exceptions.ProviderNotConfigured` if this provider
        cannot operate. Called before a campaign launches, so a misconfiguration
        surfaces before any state changes.
        """
        return None

    @abstractmethod
    def send_text(self, *, to: str, body: str, sender_id: str = "") -> SmsResult:
        """
        Send one message.

        ``sender_id`` is the alphanumeric or numeric originator the recipient
        sees. Optional because many networks ignore or override it, and several
        require it to be pre-registered — which is the gateway's problem to
        report, not this interface's to model.
        """

    def parse_delivery_report(self, payload: dict) -> list[SmsStatus]:
        """Translate a gateway callback into statuses. Unknown shapes yield []."""
        return []

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.__class__.__name__} name={self.name!r} simulated={self.is_simulated}>"
