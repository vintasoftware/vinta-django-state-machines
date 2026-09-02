"""The handful of enums this app keeps as real enums."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class ScopeType(models.TextChoices):
    """Whether a machine governs one tenant or the installation at large.

    ``GLOBAL`` is the fallback every tenant resolves to when it has not been given a
    machine of its own; ``SCOPED`` is everything belonging to one organization,
    workspace, or whatever the installing project's boundary is called.

    Kept identical to ``vinta_audit_logs.constants.ScopeType`` on purpose: a project
    running both libraries points ``STATE_MACHINES_SCOPE_MODEL`` and
    ``AUDIT_SCOPE_MODEL`` at one model, which only works while both apps ask it for the
    same values.
    """

    GLOBAL = "global", _("Global")
    SCOPED = "scoped", _("Scoped")


class IdentityType(models.TextChoices):
    """The kinds of acting principal this app ships with.

    Deliberately *not* passed as ``choices`` on the identity model: a project installing
    this app has principals of its own -- a webhook sender, an inbound email, a
    scheduled job -- and should be able to record them without a migration or a fork.
    These are only the values the app itself produces.

    Kept identical to ``vinta_audit_logs.constants.IdentityType``; see
    :class:`ScopeType` for why.
    """

    USER = "user", _("User")
    SYSTEM = "system", _("System")
    SERVICE = "service", _("Service")


class Lifecycle(models.TextChoices):
    """Publication lifecycle of a :class:`StateMachineVersion`.

    This is the one deliberate enum left in the design: a state machine cannot govern
    its own publication without infinite recursion.
    """

    DRAFT = "draft", _("Draft")
    PUBLISHED = "published", _("Published")
    ARCHIVED = "archived", _("Archived")


class StateColor(models.TextChoices):
    """Per-version presentation hint for a state."""

    NEUTRAL = "neutral", _("Neutral")
    INFO = "info", _("Info")
    SUCCESS = "success", _("Success")
    WARNING = "warning", _("Warning")
    DANGER = "danger", _("Danger")
    MUTED = "muted", _("Muted")


class HookTiming(models.TextChoices):
    """Whether a side effect runs before or after the status change is committed."""

    BEFORE = "before", _("Before")
    AFTER = "after", _("After")


class HookEvent(models.TextChoices):
    """What a side effect is bound to."""

    TRANSITION = "transition", _("A specific transition")
    ANY_TRANSITION = "any_transition", _("Any transition of the version")
    ENTER_STATE = "enter_state", _("Entering a specific state")
    LEAVE_STATE = "leave_state", _("Leaving a specific state")


class BatchLifecycle(models.TextChoices):
    """Where a :class:`~vinta_state_machines.models.StatusBatch` is in its own life.

    ``OPEN`` accepts children and their reports.  ``JOINING`` has been claimed by
    exactly one worker, which is on its way to move the parent.  ``CLOSED`` moved it.
    ``ABANDONED`` never will -- the parent was cancelled out from under the batch, or
    the join failed so persistently that giving up was the only move left.

    A timeout is deliberately *not* here: a timed-out batch still has to move its
    parent out of the waiting state, so it goes to ``JOINING`` like any other ending
    and says why in ``failure_reason``.
    """

    OPEN = "open", _("Open")
    JOINING = "joining", _("Joining")
    CLOSED = "closed", _("Closed")
    ABANDONED = "abandoned", _("Abandoned")

    @classmethod
    def live(cls) -> tuple[str, ...]:
        """The values that mean "this batch still owns its record"."""
        return (cls.OPEN.value, cls.JOINING.value)


class BatchFailureReason(models.TextChoices):
    """Why a batch ended badly, as a short code a guard can compare against.

    Deliberately *not* passed as ``choices`` on the column, for the same reason
    :class:`IdentityType` is not: a project will have reasons of its own -- a quota,
    an upstream outage, a kill switch -- and should be able to record them without a
    migration.  These are only the values the app itself produces.

    The human-readable half lives in ``failure_detail``, which nothing branches on.
    """

    TIMEOUT = "timeout", _("Timed out")
    CANCELLED = "cancelled", _("Cancelled")
    JOIN_FAILED = "join_failed", _("Join failed")
