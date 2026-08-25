"""The transition engine: what a record may do, and how it does it.

Every public function takes the record plus the *name of its status field*, because one
model can carry several independently governed statuses (``status_key``,
``engagement_status_key``, ...), each pinned to its own machine and version.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.db import models, transaction

from vinta_state_machines.conf import get_setting
from vinta_state_machines.enums import HookEvent, HookTiming
from vinta_state_machines.exceptions import (
    ApprovalRequired,
    GuardFailed,
    InvalidVersionState,
    NoStateMachineVersion,
    PermissionDenied,
    StateMachineError,
    TransitionNotAllowed,
    UnknownStatus,
)
from vinta_state_machines.fields import StatusFieldConfig, get_status_field_config
from vinta_state_machines.guards import GuardSyntaxError, evaluate
from vinta_state_machines.models import (
    StateMachine,
    StateMachineVersion,
    StatusTransition,
)
from vinta_state_machines.scopes import resolve_machine
from vinta_state_machines.side_effects import SideEffectContext, run_hooks

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from vinta_state_machines.graph import HookSpec, TransitionSpec, VersionGraph

__all__ = [
    "AvailableTransition",
    "available_actions",
    "available_transitions",
    "can_transition",
    "current_state",
    "graph_for",
    "initial_status_key",
    "resolve_version",
    "transition",
]


@dataclass(frozen=True)
class AvailableTransition:
    """One edge leaving the record's current state, with the verdict for this caller."""

    transition: TransitionSpec
    allowed: bool
    reason: str = ""

    @property
    def name(self) -> str:
        return self.transition.name

    @property
    def action(self) -> str:
        return self.transition.action

    @property
    def to_status(self) -> str:
        return self.transition.to_key

    @property
    def requires_approval(self) -> bool:
        return self.transition.requires_approval

    def __str__(self) -> str:
        return str(self.transition)


# ------------------------------------------------------------------- resolution


def resolve_version(instance: models.Model, field_name: str = "status_key") -> StateMachineVersion:
    """Return the version this record's status field is pinned to.

    Falls back to the machine's ``default_version`` when the record has no pin yet,
    which is what makes a freshly built, unsaved instance usable.
    """
    config = get_status_field_config(type(instance), field_name)
    pinned: StateMachineVersion | None = getattr(instance, config.version_field, None)
    if pinned is not None:
        return pinned
    machine = _get_machine(config, instance)
    default = machine.default_version
    if default is None:
        raise NoStateMachineVersion(
            f"State machine {config.machine_key!r} has no default_version, and "
            f"{type(instance).__name__}.{config.version_field} is not pinned."
        )
    return default


def graph_for(instance: models.Model, field_name: str = "status_key") -> VersionGraph:
    """The frozen graph governing ``instance.<field_name>``."""
    return resolve_version(instance, field_name).graph()


def current_state(instance: models.Model, field_name: str = "status_key") -> Any:
    """The :class:`StateSpec` the record currently sits on, or ``None`` before creation."""
    status_key = getattr(instance, field_name, None)
    if not status_key:
        return None
    return graph_for(instance, field_name).state(status_key)


def initial_status_key(
    instance_or_model: models.Model | type[models.Model],
    field_name: str = "status_key",
    *,
    version: StateMachineVersion | None = None,
) -> str | None:
    """The status a new record should start on, i.e. the lowest ordered initial state."""
    model = instance_or_model if isinstance(instance_or_model, type) else type(instance_or_model)
    config = get_status_field_config(model, field_name)
    if version is None:
        probe = instance_or_model if not isinstance(instance_or_model, type) else None
        version = _get_machine(config, probe).default_version
        if version is None:
            return None
    initial = version.graph().initial_states
    return initial[0].key if initial else None


def _get_machine(config: StatusFieldConfig, instance: models.Model | None = None) -> StateMachine:
    """The machine governing ``config``, for this record's tenant when there is one."""
    machine = resolve_machine(config, instance)
    if machine is None:
        raise NoStateMachineVersion(
            f"No StateMachine is registered under the key {config.machine_key!r}."
        )
    return machine


# ------------------------------------------------------------------- inspection


def available_transitions(
    instance: models.Model,
    field_name: str = "status_key",
    *,
    user: Any = None,
    metadata: Mapping[str, Any] | None = None,
    include_blocked: bool = False,
    enforce_permissions: bool = True,
) -> list[AvailableTransition]:
    """Every edge leaving the record's current state.

    By default only edges this caller could actually take are returned.  Pass
    ``include_blocked=True`` to get the blocked ones too, each carrying the reason it is
    blocked — which is what you want when rendering a UI that greys buttons out rather
    than hiding them.
    """
    graph = graph_for(instance, field_name)
    status_key = getattr(instance, field_name, None) or None
    if status_key is not None and not graph.has_state(status_key):
        raise UnknownStatus(
            f"{status_key!r} is not a state of {graph.machine_key}@{graph.version_label}."
        )

    results: list[AvailableTransition] = []
    for spec in graph.transitions_from(status_key):
        reason = _blocking_reason(
            instance,
            spec,
            graph,
            user=user,
            metadata=metadata,
            enforce_permissions=enforce_permissions,
        )
        allowed = reason == ""
        if allowed or include_blocked:
            results.append(AvailableTransition(transition=spec, allowed=allowed, reason=reason))
    return results


def available_actions(
    instance: models.Model, field_name: str = "status_key", *, user: Any = None
) -> list[str]:
    """Just the action keys the caller may fire right now."""
    return [item.action for item in available_transitions(instance, field_name, user=user)]


def can_transition(
    instance: models.Model,
    action: str,
    field_name: str = "status_key",
    *,
    user: Any = None,
    metadata: Mapping[str, Any] | None = None,
    transition_name: str | None = None,
    enforce_permissions: bool = True,
) -> bool:
    """Whether ``action`` would be accepted right now, without performing it.

    When several edges share the action, one of them being viable is enough.
    """
    graph = graph_for(instance, field_name)
    status_key = getattr(instance, field_name, None) or None
    try:
        found = _candidates(graph, status_key, action, transition_name)
    except TransitionNotAllowed:
        return False
    for spec in found:
        reason = _blocking_reason(
            instance,
            spec,
            graph,
            user=user,
            metadata=metadata,
            enforce_permissions=enforce_permissions,
        )
        # An approval requirement is a "yes, but": the edge itself is available.
        if reason in ("", _APPROVAL_REASON):
            return True
    return False


_APPROVAL_REASON = "requires approval"


def _candidates(
    graph: VersionGraph,
    status_key: str | None,
    action: str,
    transition_name: str | None = None,
) -> tuple[TransitionSpec, ...]:
    """Every edge that ``action`` could mean from here, in resolution order.

    More than one is legal — two states may be joined by several edges — so the caller
    walks them and takes the first whose guard and permission both hold.  Naming one
    explicitly narrows the answer to exactly that edge.
    """
    if status_key is not None and not graph.has_state(status_key):
        raise UnknownStatus(
            f"{status_key!r} is not a state of {graph.machine_key}@{graph.version_label}."
        )
    if transition_name is not None:
        spec = graph.named(status_key, transition_name)
        if spec is None:
            known = ", ".join(graph.names_from(status_key)) or "<none>"
            raise TransitionNotAllowed(
                f"{graph.machine_key}@{graph.version_label} declares no transition named "
                f"{transition_name!r} leaving {status_key or '*'}. Available: {known}."
            )
        if spec.action != action:
            raise TransitionNotAllowed(
                f"Transition {transition_name!r} is driven by {spec.action!r}, not {action!r}."
            )
        return (spec,)

    found = graph.candidates(status_key, action)
    if not found:
        known = ", ".join(graph.actions_from(status_key)) or "<none>"
        raise TransitionNotAllowed(
            f"{graph.machine_key}@{graph.version_label} declares no transition "
            f"{status_key or '*'} --{action}-->. Available from here: {known}."
        )
    return found


def _blocking_reason(
    instance: models.Model,
    spec: TransitionSpec,
    graph: VersionGraph,
    *,
    user: Any,
    metadata: Mapping[str, Any] | None,
    enforce_permissions: bool,
) -> str:
    source = graph.state(spec.from_key) if spec.from_key else None
    if source is not None and source.is_terminal:
        return f"{source.key} is a terminal state"
    if (
        enforce_permissions
        and spec.required_permission
        and not _has_permission(user, spec.required_permission, instance)
    ):
        return f"missing permission {spec.required_permission}"
    if spec.guard:
        try:
            if not evaluate(spec.guard, _guard_context(instance, spec, user, metadata)):
                return f"guard did not hold: {spec.guard}"
        except GuardSyntaxError as exc:
            return f"guard is invalid: {exc}"
    if spec.requires_approval:
        return _APPROVAL_REASON
    return ""


def _guard_context(
    instance: models.Model,
    spec: TransitionSpec,
    user: Any,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "obj": instance,
        "user": user,
        "action": spec.action,
        "from_status": spec.from_key,
        "to_status": spec.to_key,
        "metadata": dict(metadata or {}),
    }


def _has_permission(user: Any, permission: str, instance: models.Model) -> bool:
    checker = get_setting("PERMISSION_CHECKER")
    if checker is not None:
        return bool(checker(user, permission, instance))
    if user is None:
        return False
    has_perm = getattr(user, "has_perm", None)
    if has_perm is None:
        return False
    # Object level backends answer the two-argument form; ModelBackend only the one
    # argument form, and returns False whenever an object is supplied.
    return bool(has_perm(permission, instance) or has_perm(permission))


# -------------------------------------------------------------------- executing


def transition(
    instance: models.Model,
    action: str,
    field_name: str = "status_key",
    *,
    user: Any = None,
    comment: str = "",
    metadata: Mapping[str, Any] | None = None,
    approval: Any = None,
    transition_name: str | None = None,
    save: bool = True,
    update_fields: Iterable[str] | None = None,
    record_history: bool | None = None,
    enforce_permissions: bool = True,
    allow_unpublished: bool = False,
) -> StatusTransition | None:
    """Move ``instance`` along the edge that ``action`` names, or raise.

    The whole move — side effects, the status change, the history row — happens inside
    one transaction, so a failing ``before`` handler or a rejected guard leaves nothing
    behind.  Returns the :class:`StatusTransition` that was written, or ``None`` when
    history recording is off.

    Raises a subclass of :class:`~vinta_state_machines.exceptions.StateMachineError`:
    :class:`TransitionNotAllowed` when the version declares no such edge,
    :class:`GuardFailed`, :class:`PermissionDenied`, or :class:`ApprovalRequired`.
    """
    config = get_status_field_config(type(instance), field_name)
    version = resolve_version(instance, field_name)
    if not allow_unpublished and version.lifecycle not in get_setting("TRANSITIONABLE_LIFECYCLES"):
        raise InvalidVersionState(
            f"{version.state_machine.key}@{version.version} is {version.lifecycle}; "
            "records can only move under a published version."
        )

    graph = version.graph()
    from_key = getattr(instance, field_name, None) or None
    found = _candidates(graph, from_key, action, transition_name)

    source = graph.state(from_key) if from_key else None
    if source is not None and source.is_terminal:
        raise TransitionNotAllowed(
            f"{source.key} is a terminal state of {graph.machine_key}@{graph.version_label}."
        )

    spec = _select(instance, found, user=user, metadata=metadata, enforce=enforce_permissions)

    if spec.requires_approval and approval is None:
        raise ApprovalRequired(
            f"Transition {spec} requires an approval; pass approval=... to commit it.",
            transition=spec,
        )

    target = graph.state(spec.to_key)
    if target is None:  # pragma: no cover - build_graph filters these out
        raise UnknownStatus(f"{spec.to_key!r} is not a state of this version.")

    should_record = get_setting("RECORD_HISTORY") if record_history is None else record_history
    payload = dict(metadata or {})
    touched: set[str] = set()

    def make_context(
        hook: HookSpec, timing: str, event: str, record: StatusTransition | None
    ) -> SideEffectContext:
        return SideEffectContext(
            instance=instance,
            field_name=field_name,
            status_field=graph.status_field,
            from_status=from_key,
            to_status=spec.to_key,
            action=spec.action,
            version=version,
            graph=graph,
            transition=spec,
            timing=timing,
            event=event,
            hook=hook,
            actor=user,
            params=hook.params,
            metadata=payload,
            record=record,
            touched=touched,
        )

    with transaction.atomic():
        _fire(graph, HookTiming.BEFORE, spec, from_key, make_context, None)

        setattr(instance, field_name, spec.to_key)
        touched_fields = [field_name]
        if getattr(instance, f"{config.version_field}_id", None) is None:
            setattr(instance, config.version_field, version)
            touched_fields.append(config.version_field)

        if save:
            if instance.pk is None:
                instance.save()
            else:
                fields = list(update_fields) if update_fields is not None else touched_fields
                # Whatever a ``before`` handler changed rides along with the status write.
                instance.save(update_fields=_merge(fields, touched))
        written = set(touched)

        record = (
            _write_history(
                instance,
                graph=graph,
                version=version,
                spec=spec,
                from_key=from_key,
                user=user,
                comment=comment,
                metadata=payload,
                approval=approval,
            )
            if should_record
            else None
        )

        _fire(graph, HookTiming.AFTER, spec, from_key, make_context, record)

        # An ``after`` handler runs past the status write, so anything it touched needs
        # a second, targeted save inside the same transaction.
        late = touched - written
        if save and late and instance.pk is not None:
            instance.save(update_fields=sorted(late))

    return record


def _merge(fields: list[str], extra: set[str]) -> list[str]:
    merged = list(fields)
    merged.extend(sorted(name for name in extra if name not in merged))
    return merged


def _select(
    instance: models.Model,
    found: tuple[TransitionSpec, ...],
    *,
    user: Any,
    metadata: Mapping[str, Any] | None,
    enforce: bool,
) -> TransitionSpec:
    """Pick the edge to take, walking the candidates in order.

    With one candidate this just re-raises its own specific failure, which keeps the
    error messages precise.  With several, the first viable one wins, and if none are
    viable the caller hears about the one declared first.
    """
    first_error: StateMachineError | None = None
    for spec in found:
        try:
            _assert_viable(instance, spec, user=user, metadata=metadata, enforce=enforce)
        except StateMachineError as exc:
            first_error = first_error or exc
            continue
        return spec
    assert first_error is not None
    raise first_error


def _assert_viable(
    instance: models.Model,
    spec: TransitionSpec,
    *,
    user: Any,
    metadata: Mapping[str, Any] | None,
    enforce: bool,
) -> None:
    """Raise unless ``spec`` clears its permission and its guard."""
    if (
        enforce
        and spec.required_permission
        and not _has_permission(user, spec.required_permission, instance)
    ):
        raise PermissionDenied(
            f"Transition {spec} requires the permission "
            f"{spec.required_permission!r}, which the actor does not hold."
        )
    if not spec.guard:
        return
    try:
        passed = evaluate(spec.guard, _guard_context(instance, spec, user, metadata))
    except GuardSyntaxError as exc:
        raise GuardFailed(
            f"Guard of transition {spec} could not be evaluated: {exc}", guard=spec.guard
        ) from exc
    if not passed:
        raise GuardFailed(
            f"Guard of transition {spec} did not hold: {spec.guard}", guard=spec.guard
        )


def _fire(
    graph: VersionGraph,
    timing: str,
    spec: TransitionSpec,
    from_key: str | None,
    make_context: Any,
    record: StatusTransition | None,
) -> None:
    """Run the hooks for one timing: leave the old state, cross the edge, enter the new.

    The order is deliberate and mirrored on both sides, so a pair of ``before`` and
    ``after`` handlers on the same binding always bracket the change symmetrically.
    """
    plans = (
        (HookEvent.LEAVE_STATE, graph.hooks_for_state(timing, HookEvent.LEAVE_STATE, from_key)),
        (HookEvent.TRANSITION, graph.hooks_for_transition(timing, spec.pk)),
        (
            HookEvent.ENTER_STATE,
            graph.hooks_for_state(timing, HookEvent.ENTER_STATE, spec.to_key),
        ),
    )
    for event, hooks in plans:
        if hooks:
            run_hooks(list(hooks), _context_factory(make_context, timing, event, record))


def _context_factory(
    make_context: Any, timing: str, event: str, record: StatusTransition | None
) -> Callable[[HookSpec], SideEffectContext]:
    def build(hook: HookSpec) -> SideEffectContext:
        result: SideEffectContext = make_context(hook, timing, event, record)
        return result

    return build


def _write_history(
    instance: models.Model,
    *,
    graph: VersionGraph,
    version: StateMachineVersion,
    spec: TransitionSpec,
    from_key: str | None,
    user: Any,
    comment: str,
    metadata: Mapping[str, Any],
    approval: Any,
) -> StatusTransition:
    from django.contrib.contenttypes.models import ContentType

    source = graph.state(from_key) if from_key else None
    target = graph.state(spec.to_key)
    assert target is not None
    payload = dict(metadata)
    if approval is not None:
        payload.setdefault("approval", _describe(approval))
    return StatusTransition.objects.create(
        target_type=ContentType.objects.get_for_model(instance, for_concrete_model=False),
        target_id=str(instance.pk),
        status_field=graph.status_field,
        from_status_id=source.status_pk if source else None,
        to_status_id=target.status_pk,
        state_machine_version=version,
        scope_id=graph.scope_pk,
        action_type_id=spec.action_pk,
        transition_id=spec.pk,
        actor=user if _is_saved_user(user) else None,
        comment=comment,
        metadata=payload,
    )


def _is_saved_user(user: Any) -> bool:
    return (
        isinstance(user, models.Model)
        and user.pk is not None
        and getattr(user, "is_authenticated", True)
    )


def _describe(approval: Any) -> Any:
    if isinstance(approval, models.Model):
        return {"model": approval._meta.label_lower, "pk": str(approval.pk)}
    if isinstance(approval, (str, int, float, bool)) or approval is None:
        return approval
    return str(approval)
