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
