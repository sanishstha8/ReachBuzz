"""
Who may look across the tenant boundary, and what happens when they do.

Every other part of this application spends its effort making sure one customer
cannot see another's data. This app is the deliberate exception, and the whole
of it hangs on the two decisions in this module.

**The gate is ``is_staff``, not ``User.role``.** ``role`` says what somebody may
do inside the product; ``is_staff`` says they work for whoever runs it. They are
not the same thing, and Stage 1 separated them precisely so a customer's own
administrator could never end up reading somebody else's data. ``is_staff`` is
settable only through Django's admin, which already requires ``is_staff`` — so
this app grants no capability its users did not already have. It is a better
window onto data they can reach anyway, not a wider one.

**Looking is logged.** Opening a customer's page is a privacy event, not a page
view. It is recorded against the organization that was looked at, naming the
person who looked, because "who has read this customer's account?" is a question
that has to have an answer.

Two things this app deliberately cannot do, enforced by never building them:

* **No message content.** Counts, statuses and totals, never a line a customer
  wrote to one of their contacts. Support work needs aggregates; reading
  correspondence is a different power and nobody has asked for it.
* **No impersonation.** There is no "sign in as this customer" button.
"""

from __future__ import annotations

import logging

from django.contrib.auth.mixins import AccessMixin
from django.http import Http404

from core.audit import record_audit
from core.models import AuditAction

logger = logging.getLogger(__name__)


class StaffOnlyMixin(AccessMixin):
    """
    Refuses anyone who does not work for the platform.

    **404, not 403, for a signed-in customer who guesses the URL.** A 403
    confirms that something exists at that address; a 404 says nothing. An
    anonymous visitor gets the ordinary sign-in redirect instead, because that
    leaks nothing the login page does not.
    """

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        if not request.user.is_staff:
            logger.warning(
                "Non-staff user %s tried to reach the backoffice at %s",
                request.user.pk,
                request.path,
            )
            raise Http404("No such page.")

        self.after_staff_check(request)
        return super().dispatch(request, *args, **kwargs)

    def after_staff_check(self, request) -> None:
        """
        Hook for anything that must happen once, after the gate and before the
        view. A no-op here; :class:`RecordsTheLookMixin` writes the audit entry.

        A hook rather than a second ``dispatch`` override so the staff check
        runs exactly once and the ordering is impossible to get wrong.
        """
        return None


class RecordsTheLookMixin(StaffOnlyMixin):
    """
    For views that show one identified customer's data.

    Not used on the aggregate pages. A count of organizations is not a look at
    any particular customer, and auditing every dashboard refresh would bury the
    entries that matter under ones that do not.

    The entry is written *before* the page renders, so a template error halfway
    through does not lose the record of what was opened.
    """

    def audit_target(self):
        """The organization being looked at. Each view supplies its own."""
        raise NotImplementedError

    def after_staff_check(self, request) -> None:
        organization = self.audit_target()
        if organization is None:
            return

        record_audit(
            AuditAction.BACKOFFICE_VIEWED,
            user=request.user,
            request=request,
            obj=organization,
            description=f"Viewed the account for {organization.name}",
            metadata={"organization": str(organization.pk), "path": request.path},
        )
