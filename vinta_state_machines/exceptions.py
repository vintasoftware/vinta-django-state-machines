"""Exception hierarchy raised by the transition engine."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError

if TYPE_CHECKING:
    from vinta_state_machines.graph import TransitionSpec


class StateMachineError(ValidationError):
    """Base class for every error raised by this app.

    It subclasses :class:`django.core.exceptions.ValidationError` so that transition
    failures surface naturally through forms, serializers and ``full_clean()``.
    """

    default_code = "state_machine_error"

    def __init__(self, message: str, *, code: str | None = None, **params: Any) -> None:
        super().__init__(message, code=code or self.default_code, params=params or None)
        self.message_text = message

    def __str__(self) -> str:
        return self.message_text


class NoStateMachineVersion(StateMachineError):
    """No usable :class:`StateMachineVersion` could be resolved for a record."""

    default_code = "no_state_machine_version"


class InvalidVersionState(StateMachineError):
    """A version is not in a lifecycle state that allows the requested operation."""

    default_code = "invalid_version_state"


class UnknownStatus(StateMachineError):
    """A status key is not declared as a state of the pinned version."""

    default_code = "unknown_status"


class UnknownAction(StateMachineError):
    """An action key is not part of the shared action vocabulary."""

    default_code = "unknown_action"


class TransitionNotAllowed(StateMachineError):
    """The pinned version declares no edge for ``(from_status, action)``."""

    default_code = "transition_not_allowed"


class GuardFailed(StateMachineError):
    """A transition exists but its guard expression evaluated to false."""

    default_code = "guard_failed"

    def __init__(self, message: str, *, guard: str = "", **params: Any) -> None:
        super().__init__(message, **params)
        self.guard = guard


class PermissionDenied(StateMachineError):
    """The acting principal lacks ``required_permission`` for the transition."""

    default_code = "permission_denied"


class ApprovalRequired(StateMachineError):
    """The transition is flagged ``requires_approval`` and none was supplied."""

    default_code = "approval_required"

    def __init__(self, message: str, *, transition: TransitionSpec | None = None) -> None:
        super().__init__(message)
        self.transition = transition


class BatchDepthExceeded(StateMachineError):
    """A fan-out tried to nest deeper than ``MAX_BATCH_DEPTH`` allows.

    Almost always a machine whose child machine is, transitively, itself.  Without the
    cap that recurses until something else gives out.
    """

    default_code = "batch_depth_exceeded"
