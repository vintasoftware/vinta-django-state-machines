"""Data-driven, versioned state machines for Django.

Status is modelled as *data* rather than as an enum scattered across tables:

* :class:`~vinta_state_machines.models.StatusDefinition` is the shared vocabulary of
  status values for a ``(entity_type, status_field)`` pair.
* :class:`~vinta_state_machines.models.StateMachine` owns a stream of immutable
  :class:`~vinta_state_machines.models.StateMachineVersion` graphs.  Each version
  declares its valid states and its guarded transitions.
* :class:`~vinta_state_machines.models.ActionType` is the shared verb vocabulary that
  drives transitions.
* Every status bearing row pins the version it was created under, so publishing a new
  version never migrates or invalidates existing data.
"""

from vinta_state_machines.exceptions import (
    ApprovalRequired,
    CapabilityDenied,
    GuardFailed,
    InvalidVersionState,
    NoStateMachineVersion,
    PermissionDenied,
    StateMachineError,
    TransitionNotAllowed,
    UnknownAction,
    UnknownStatus,
)

__all__ = [
    "ApprovalRequired",
    "CapabilityDenied",
    "GuardFailed",
    "InvalidVersionState",
    "NoStateMachineVersion",
    "PermissionDenied",
    "StateMachineError",
    "TransitionNotAllowed",
    "UnknownAction",
    "UnknownStatus",
    "__version__",
    "default_app_config",
]

__version__ = "0.6.0"

default_app_config = "vinta_state_machines.apps.StateMachinesConfig"
