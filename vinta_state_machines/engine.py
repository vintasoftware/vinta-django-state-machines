"""The transition engine: what a record may do, and how it does it.

Every public function takes the record plus the *name of its status field*, because one
model can carry several independently governed statuses (``status_key``,
``engagement_status_key``, ...), each pinned to its own machine and version.

They also take an ``actor``: whoever is trying to move the record.  The engine keeps it
*live* for as long as it is deciding -- permissions and guards need an object that can
answer ``has_perm`` -- and only snapshots it into an identity row at the moment the move
is written down.  See :mod:`vinta_state_machines.identities` for the two halves of that.
Anything that module understands works here: a user, an identity row, an
``IdentitySnapshot``, or ``None`` for the system.

Every public function has an ``a``-prefixed twin -- :func:`atransition`,
:func:`acan_transition`, :func:`aavailable_transitions` and so on -- for callers that
are already on an event loop.  See :ref:`the async section <async-engine>` below.

.. _async-engine:

Async
-----

The async twins exist for two reasons: an async view cannot call into the ORM directly
without ``SynchronousOnlyOperation``, and an ``async def`` side effect needs a real loop
to be awaited on rather than a throwaway one.

Each twin is the synchronous function moved onto a thread-sensitive executor::

    async def atransition(...):
        return await sync_to_async(transition, thread_sensitive=True)(...)

That is the whole implementation, and the shape is chosen rather than settled for.
Django has no async transaction API -- there is no ``await transaction.aatomic()`` in
5.2 or 6.0 -- so a genuinely async engine would have to give up ``atomic()``, and with
it the single guarantee the whole module is built on: side effects, the status write and
the history row commit together or not at all.  Running the existing block on an
executor keeps that guarantee exactly as it is, byte for byte, with no second code path
to drift out of step with the first.

``thread_sensitive=True`` is doing real work and is not a default worth changing.  It
pins the whole move to one thread, so the ``atomic()`` block, every query inside it and
its ``on_commit`` callbacks all meet the same connection.  It is also what lets an
``async def`` hook be awaited on *the caller's* loop instead of a new one: asgiref
records the loop that entered the executor, and the ``async_to_sync`` in
:func:`~vinta_state_machines.side_effects.call_handler` hands the coroutine back to it.

What follows from that, and is worth knowing before reaching for these:

* Concurrency is the executor's, not the loop's.  ``asyncio.gather`` over a hundred
  ``atransition`` calls does not run a hundred transitions at once; it runs them as the
  thread-sensitive executor allows.  These twins are for *not blocking the loop* and for
  awaiting async hooks, not for parallelism.
* An async hook awaits with the transaction open, so slow I/O still belongs on an
  ``on_commit`` binding.
* Nothing is deprecated.  The synchronous functions are unchanged, remain the primary
  API, and are what a management command, a Celery task or a synchronous view should
  keep calling.
"""

from __future__ import annotations

import functools
import warnings
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, TypeVar, cast

from asgiref.sync import sync_to_async
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
from vinta_state_machines.identities import resolve_identity
from vinta_state_machines.instruments import (
    NULL_SPAN,
    InspectionEvent,
    TransitionEvent,
    actor_descriptor,
    filtered_metadata,
    observe,
)
from vinta_state_machines.models import (
    StateMachine,
    StateMachineVersion,
    StatusTransition,
)
from vinta_state_machines.runs import RunRecorder
from vinta_state_machines.scopes import resolve_machine
from vinta_state_machines.side_effects import SideEffectContext, run_hooks

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from vinta_state_machines.graph import HookSpec, TransitionSpec, VersionGraph

__all__ = [
    "AvailableTransition",
    "aavailable_actions",
    "aavailable_transitions",
    "acan_transition",
    "acurrent_state",
    "agraph_for",
    "ainitial_status_key",
    "aresolve_version",
    "atransition",
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


_F = TypeVar("_F", bound="Callable[..., Any]")


def _accepts_user_alias(func: _F) -> _F:
    """Accept the pre-0.2 ``user=`` spelling of ``actor=`` for one release.

    The rename is not cosmetic: the parameter now takes identities and snapshots as well
    as users, so the old name had become a lie about what it accepts.  Callers that were
    passing a user keep working unchanged and get told once, per call site.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if "user" in kwargs:
            if "actor" in kwargs:
                raise TypeError(
                    f"{func.__name__}() got both 'actor' and the deprecated 'user'. "
                    "Pass only 'actor'."
                )
            warnings.warn(
                f"{func.__name__}(user=...) is deprecated; use actor=... instead. "
                "It accepts a user, an identity row, an IdentitySnapshot, or None.",
                DeprecationWarning,
                stacklevel=2,
            )
            kwargs["actor"] = kwargs.pop("user")
        return func(*args, **kwargs)

    return cast("_F", wrapper)


# ------------------------------------------------------------------- inspection


@_accepts_user_alias
def available_transitions(
    instance: models.Model,
    field_name: str = "status_key",
    *,
    actor: Any = None,
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

    with _inspecting(instance, graph, "available_transitions", status_key) as span:
        candidates = graph.transitions_from(status_key)
        results: list[AvailableTransition] = []
        for spec in candidates:
            reason = _blocking_reason(
                instance,
                spec,
                graph,
                actor=actor,
                metadata=metadata,
                enforce_permissions=enforce_permissions,
            )
            allowed = reason == ""
            if allowed or include_blocked:
                results.append(
                    AvailableTransition(transition=spec, allowed=allowed, reason=reason)
                )
        span.set(considered=len(candidates), returned=len(results))
        return results


@_accepts_user_alias
def available_actions(
    instance: models.Model, field_name: str = "status_key", *, actor: Any = None
) -> list[str]:
    """Just the action keys the caller may fire right now."""
    return [item.action for item in available_transitions(instance, field_name, actor=actor)]


@_accepts_user_alias
def can_transition(
    instance: models.Model,
    action: str,
    field_name: str = "status_key",
    *,
    actor: Any = None,
    metadata: Mapping[str, Any] | None = None,
    transition_name: str | None = None,
    enforce_permissions: bool = True,
) -> bool:
    """Whether ``action`` would be accepted right now, without performing it.

    When several edges share the action, one of them being viable is enough.
    """
    graph = graph_for(instance, field_name)
    status_key = getattr(instance, field_name, None) or None
    with _inspecting(instance, graph, "can_transition", status_key, action) as span:
        try:
            found = _candidates(graph, status_key, action, transition_name)
        except TransitionNotAllowed:
            span.set(answer=False)
            return False
        for spec in found:
            reason = _blocking_reason(
                instance,
                spec,
                graph,
                actor=actor,
                metadata=metadata,
                enforce_permissions=enforce_permissions,
            )
            # An approval requirement is a "yes, but": the edge itself is available.
            if reason in ("", _APPROVAL_REASON):
                span.set(answer=True)
                return True
        span.set(answer=False)
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
    actor: Any,
    metadata: Mapping[str, Any] | None,
    enforce_permissions: bool,
) -> str:
    source = graph.state(spec.from_key) if spec.from_key else None
    if source is not None and source.is_terminal:
        return f"{source.key} is a terminal state"
    if (
        enforce_permissions
        and spec.required_permission
        and not _has_permission(actor, spec.required_permission, instance)
    ):
        return f"missing permission {spec.required_permission}"
    if spec.guard:
        try:
            if not evaluate(spec.guard, _guard_context(instance, spec, actor, metadata)):
                return f"guard did not hold: {spec.guard}"
        except GuardSyntaxError as exc:
            return f"guard is invalid: {exc}"
    if spec.requires_approval:
        return _APPROVAL_REASON
    return ""


def _guard_context(
    instance: models.Model,
    spec: TransitionSpec,
    actor: Any,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "obj": instance,
        "actor": actor,
        # Kept alongside ``actor`` because guard expressions live in the database:
        # renaming the key would silently change what every stored guard evaluates.
        "user": actor,
        "action": spec.action,
        "from_status": spec.from_key,
        "to_status": spec.to_key,
        "metadata": dict(metadata or {}),
    }


def _has_permission(actor: Any, permission: str, instance: models.Model) -> bool:
    checker = get_setting("PERMISSION_CHECKER")
    if checker is not None:
        return bool(checker(actor, permission, instance))
    if actor is None:
        return False
    has_perm = getattr(actor, "has_perm", None)
    if has_perm is not None:
        # Object level backends answer the two-argument form; ModelBackend only the one
        # argument form, and returns False whenever an object is supplied.
        return bool(has_perm(permission, instance) or has_perm(permission))
    return _snapshot_grants(actor, permission)


def _snapshot_grants(actor: Any, permission: str) -> bool:
    """Answer from a recorded authorization snapshot, for an actor that is not live.

    Reached when the caller passed an identity row or an ``IdentitySnapshot`` rather
    than a user -- replaying a move, or acting as a principal this app cannot
    introspect.  The answer is what the actor was allowed to do *when the snapshot was
    taken*, which is the only honest answer available: there is no live object left to
    ask, and inventing one by dereferencing ``identity.user`` would silently swap a
    historical question for a current one.
    """
    if getattr(actor, "is_superuser", False):
        return True
    keys: list[str] = list(getattr(actor, "permission_keys", None) or ())
    return permission in keys


# -------------------------------------------------------------------- executing


@_accepts_user_alias
def transition(
    instance: models.Model,
    action: str,
    field_name: str = "status_key",
    *,
    actor: Any = None,
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
    graph = version.graph()
    from_key = getattr(instance, field_name, None) or None

    # Opened here, before the lifecycle check and before an edge has been chosen, so
    # that a *refused* move is observed rather than missed -- which is the whole reason
    # this is not a hook. What the engine learns later goes on through ``span.set``.
    base_event = _transition_event(instance, graph, action, from_key, actor)
    with observe(base_event) as span:
        for key, value in filtered_metadata(metadata).items():
            span.set(**{f"metadata.{key}": value})

        if not allow_unpublished and version.lifecycle not in get_setting(
            "TRANSITIONABLE_LIFECYCLES"
        ):
            raise InvalidVersionState(
                f"{version.state_machine.key}@{version.version} is {version.lifecycle}; "
                "records can only move under a published version."
            )

        found = _candidates(graph, from_key, action, transition_name)

        source = graph.state(from_key) if from_key else None
        if source is not None and source.is_terminal:
            raise TransitionNotAllowed(
                f"{source.key} is a terminal state of {graph.machine_key}@{graph.version_label}."
            )

        spec = _select(
            instance, found, actor=actor, metadata=metadata, enforce=enforce_permissions
        )
        span.set(to_status=spec.to_key, transition_name=spec.name)
        event = replace(base_event, to_status=spec.to_key, transition_name=spec.name)

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
            hook: HookSpec, timing: str, event_name: str, record: StatusTransition | None
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
                event=event_name,
                hook=hook,
                actor=actor,
                params=hook.params,
                metadata=payload,
                record=record,
                touched=touched,
                span_id=span.id,
            )

        recorder = RunRecorder(
            instance=instance, graph=graph, version=version, spec=spec, from_key=from_key
        )
        record: StatusTransition | None = None
        try:
            with transaction.atomic():
                _fire(
                    graph, HookTiming.BEFORE, spec, from_key, make_context, None, recorder, event
                )

                setattr(instance, field_name, spec.to_key)
                touched_fields = [field_name]
                if getattr(instance, f"{config.version_field}_id", None) is None:
                    setattr(instance, config.version_field, version)
                    touched_fields.append(config.version_field)

                if save:
                    if instance.pk is None:
                        instance.save()
                    else:
                        fields = (
                            list(update_fields) if update_fields is not None else touched_fields
                        )
                        # Whatever a ``before`` handler changed rides along with the
                        # status write.
                        instance.save(update_fields=_merge(fields, touched))
                written = set(touched)

                record = (
                    _write_history(
                        instance,
                        graph=graph,
                        version=version,
                        spec=spec,
                        from_key=from_key,
                        actor=actor,
                        comment=comment,
                        metadata=payload,
                        approval=approval,
                    )
                    if should_record
                    else None
                )

                _fire(
                    graph, HookTiming.AFTER, spec, from_key, make_context, record, recorder, event
                )

                # An ``after`` handler runs past the status write, so anything it
                # touched needs a second, targeted save inside the same transaction.
                late = touched - written
                if save and late and instance.pk is not None:
                    instance.save(update_fields=sorted(late))
        except Exception:
            # The block above rolled back and took any run rows written inside it with
            # it, which is exactly the case worth keeping: flush what was measured out
            # here, unattached to a history row that no longer exists, and let the error
            # go on.
            recorder.flush_after_failure()
            raise

        recorder.flush(status_transition=record)
        if record is not None:
            # Links the trace back to the durable row, which is the join an operator
            # makes when an APM alert needs the record it happened to.
            span.set(record_pk=record.pk)
        return record


def _transition_event(
    instance: models.Model,
    graph: VersionGraph,
    action: str,
    from_key: str | None,
    actor: Any,
) -> TransitionEvent:
    """Describe a move using only keys, and without a single extra query."""
    actor_type, actor_key = actor_descriptor(actor)
    return TransitionEvent(
        machine_key=graph.machine_key,
        version_pk=graph.version_pk,
        version_label=graph.version_label,
        scope_key=graph.scope_key,
        entity_type=graph.entity_type,
        status_field=graph.status_field,
        target_label=instance._meta.label_lower,
        target_id=str(instance.pk or ""),
        action=action,
        from_status=from_key,
        actor_type=actor_type,
        actor_key=actor_key,
    )


def _inspecting(
    instance: models.Model,
    graph: VersionGraph,
    operation: str,
    status_key: str | None,
    action: str = "",
) -> Any:
    """Observe a read-only question, but only when the project asked for it."""
    if not get_setting("INSTRUMENT_INSPECTION"):
        # Carries the shared no-op span, so the call sites below never have to ask
        # whether anything is watching before annotating what they found.
        return nullcontext(NULL_SPAN)
    return observe(
        InspectionEvent(
            operation=operation,
            machine_key=graph.machine_key,
            version_pk=graph.version_pk,
            version_label=graph.version_label,
            scope_key=graph.scope_key,
            target_label=instance._meta.label_lower,
            target_id=str(instance.pk or ""),
            status_key=status_key or "",
            action=action,
        )
    )


def _merge(fields: list[str], extra: set[str]) -> list[str]:
    merged = list(fields)
    merged.extend(sorted(name for name in extra if name not in merged))
    return merged


def _select(
    instance: models.Model,
    found: tuple[TransitionSpec, ...],
    *,
    actor: Any,
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
            _assert_viable(instance, spec, actor=actor, metadata=metadata, enforce=enforce)
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
    actor: Any,
    metadata: Mapping[str, Any] | None,
    enforce: bool,
) -> None:
    """Raise unless ``spec`` clears its permission and its guard."""
    if (
        enforce
        and spec.required_permission
        and not _has_permission(actor, spec.required_permission, instance)
    ):
        raise PermissionDenied(
            f"Transition {spec} requires the permission "
            f"{spec.required_permission!r}, which the actor does not hold."
        )
    if not spec.guard:
        return
    try:
        passed = evaluate(spec.guard, _guard_context(instance, spec, actor, metadata))
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
    recorder: RunRecorder | None = None,
    transition_event: TransitionEvent | None = None,
) -> None:
    """Run the hooks for one timing: leave the old state, cross the edge, enter the new.

    The order is deliberate and mirrored on both sides, so a pair of ``before`` and
    ``after`` handlers on the same binding always bracket the change symmetrically.

    ``transition_event`` is what a handler's own span hangs off, so it is passed down
    rather than rebuilt: one description of the move, shared by every span under it.
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
            run_hooks(
                list(hooks),
                _context_factory(make_context, timing, event, record),
                recorder=recorder,
                record=record,
                transition_event=transition_event,
            )


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
    actor: Any,
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
    # Snapshotted here, inside the transaction that commits the move, so the identity
    # and the history row it belongs to are written or rolled back together.
    identity = resolve_identity(actor)
    return StatusTransition.objects.create(
        target_type=ContentType.objects.get_for_model(instance, for_concrete_model=False),
        target_id=str(instance.pk),
        status_field=graph.status_field,
        from_status_id=source.status_pk if source else None,
        to_status_id=target.status_pk,
        state_machine_version=version,
        scope_id=graph.scope_pk,
        scope_key=graph.scope_key,
        action_type_id=spec.action_pk,
        transition_id=spec.pk,
        actor=identity,
        actor_type=identity.identity_type,
        actor_key=identity.identity_key,
        comment=comment,
        metadata=payload,
    )


def _describe(approval: Any) -> Any:
    if isinstance(approval, models.Model):
        return {"model": approval._meta.label_lower, "pk": str(approval.pk)}
    if isinstance(approval, (str, int, float, bool)) or approval is None:
        return approval
    return str(approval)


# ----------------------------------------------------------------------- async

# The twins, for callers already on an event loop. Each one is its synchronous
# counterpart moved onto a thread-sensitive executor, and nothing else: the module
# docstring explains why that is the design rather than a stopgap, and why
# ``thread_sensitive=True`` is load-bearing in every one of them.
#
# Signatures are written out rather than generated with a ``**kwargs`` forwarder so that
# the parameters, defaults and return types survive into mypy and into an editor. The
# one thing deliberately not carried over is the pre-0.2 ``user=`` alias: it exists to
# keep old call sites working, and there are no old async call sites.


async def aresolve_version(
    instance: models.Model, field_name: str = "status_key"
) -> StateMachineVersion:
    """Await :func:`resolve_version`."""
    return await sync_to_async(resolve_version, thread_sensitive=True)(instance, field_name)


async def agraph_for(instance: models.Model, field_name: str = "status_key") -> VersionGraph:
    """Await :func:`graph_for`."""
    return await sync_to_async(graph_for, thread_sensitive=True)(instance, field_name)


async def acurrent_state(instance: models.Model, field_name: str = "status_key") -> Any:
    """Await :func:`current_state`."""
    return await sync_to_async(current_state, thread_sensitive=True)(instance, field_name)


async def ainitial_status_key(
    instance_or_model: models.Model | type[models.Model],
    field_name: str = "status_key",
    *,
    version: StateMachineVersion | None = None,
) -> str | None:
    """Await :func:`initial_status_key`."""
    return await sync_to_async(initial_status_key, thread_sensitive=True)(
        instance_or_model, field_name, version=version
    )


async def aavailable_transitions(
    instance: models.Model,
    field_name: str = "status_key",
    *,
    actor: Any = None,
    metadata: Mapping[str, Any] | None = None,
    include_blocked: bool = False,
    enforce_permissions: bool = True,
) -> list[AvailableTransition]:
    """Await :func:`available_transitions`."""
    return await sync_to_async(available_transitions, thread_sensitive=True)(
        instance,
        field_name,
        actor=actor,
        metadata=metadata,
        include_blocked=include_blocked,
        enforce_permissions=enforce_permissions,
    )


async def aavailable_actions(
    instance: models.Model, field_name: str = "status_key", *, actor: Any = None
) -> list[str]:
    """Await :func:`available_actions`."""
    return await sync_to_async(available_actions, thread_sensitive=True)(
        instance, field_name, actor=actor
    )


async def acan_transition(
    instance: models.Model,
    action: str,
    field_name: str = "status_key",
    *,
    actor: Any = None,
    metadata: Mapping[str, Any] | None = None,
    transition_name: str | None = None,
    enforce_permissions: bool = True,
) -> bool:
    """Await :func:`can_transition`."""
    return await sync_to_async(can_transition, thread_sensitive=True)(
        instance,
        action,
        field_name,
        actor=actor,
        metadata=metadata,
        transition_name=transition_name,
        enforce_permissions=enforce_permissions,
    )


async def atransition(
    instance: models.Model,
    action: str,
    field_name: str = "status_key",
    *,
    actor: Any = None,
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
    """Await :func:`transition`: move ``instance`` along the edge ``action`` names.

    Identical in every observable way to the synchronous call -- same edge resolution,
    same guards, same one ``atomic()`` block, same exceptions, same return value -- with
    two differences that matter to an async caller:

    * It does not raise ``SynchronousOnlyOperation``, because the ORM work happens off
      the loop.
    * An ``async def`` side effect bound to this move is awaited on *this* loop, rather
      than on a throwaway one created for the call.  That is the reason to prefer this
      entry point over ``sync_to_async(transition)`` written out by hand at the call
      site: the hand-written version works, but every async hook underneath it pays for
      a fresh event loop.

    ``instance`` is mutated in place exactly as it is synchronously, so its status field
    and version pin are up to date when the await returns.
    """
    return await sync_to_async(transition, thread_sensitive=True)(
        instance,
        action,
        field_name,
        actor=actor,
        comment=comment,
        metadata=metadata,
        approval=approval,
        transition_name=transition_name,
        save=save,
        update_fields=update_fields,
        record_history=record_history,
        enforce_permissions=enforce_permissions,
        allow_unpublished=allow_unpublished,
    )
