"""The handful of enums this app keeps as real enums."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


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
