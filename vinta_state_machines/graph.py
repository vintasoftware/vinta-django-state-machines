"""The immutable, in-memory read model of one :class:`StateMachineVersion`.

Loading a version's states, transitions and hooks once and freezing them keeps the
engine's hot path free of queries, and gives validation a single place to reason about
a graph's shape.  Graphs are cached per version and invalidated whenever any row of
that version changes.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vinta_state_machines.conf import get_setting
from vinta_state_machines.enums import HookEvent, HookTiming

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime

    from vinta_state_machines.models import (
        StateMachineHook,
        StateMachineState,
        StateMachineVersion,
    )

CREATION = None
"""The ``from_state`` of a creation edge: there is no previous status."""


@dataclass(frozen=True)
class StateSpec:
    """One state of a version, flattened."""

    pk: int
    status_pk: int
    key: str
    name: str
    is_initial: bool
    is_terminal: bool
    color: str
    order: int
    x: int
    y: int

    @property
    def position(self) -> tuple[int, int]:
        """Where this state sits on the version's canvas."""
        return (self.x, self.y)

    def __str__(self) -> str:
        return self.key


@dataclass(frozen=True)
class TransitionSpec:
    """One named, guarded edge of a version, flattened."""

    pk: int
    name: str
    from_key: str | None
    to_key: str
    action: str
    action_pk: int
    guard: str
    required_permission: str
    requires_approval: bool
    order: int
    description: str

    @property
    def is_creation(self) -> bool:
        return self.from_key is CREATION

    @property
    def is_self_transition(self) -> bool:
        """An edge that leaves a state and arrives back at the same one."""
        return self.from_key is not CREATION and self.from_key == self.to_key

    def __str__(self) -> str:
        return f"{self.name} ({self.from_key or '*'} --{self.action}--> {self.to_key})"


@dataclass(frozen=True)
class HookSpec:
    """One side-effect binding of a version, flattened."""

    pk: int
    handler_key: str
    timing: str
    event: str
    transition_pk: int | None
    state_key: str | None
    params: dict[str, object]
    order: int
    on_commit: bool

    def __str__(self) -> str:
        return f"{self.timing}:{self.event} -> {self.handler_key}"


@dataclass(frozen=True)
class VersionGraph:
    """Everything the engine needs to decide whether a move is legal."""

    version_pk: int
    version_label: str
    lifecycle: str
    machine_key: str
    entity_type: str
    status_field: str
    modified_at: datetime | None
    scope_pk: Any = None
    """The tenant this machine belongs to, carried so a history row can be stamped
    without re-reading the machine on every transition."""
    states: dict[str, StateSpec] = field(default_factory=dict)
    transitions: tuple[TransitionSpec, ...] = ()
    _by_source: dict[str | None, tuple[TransitionSpec, ...]] = field(default_factory=dict)
    _by_action: dict[tuple[str | None, str], tuple[TransitionSpec, ...]] = field(
        default_factory=dict
    )
    _by_name: dict[tuple[str | None, str], TransitionSpec] = field(default_factory=dict)
    _hooks: dict[tuple[str, str, object], tuple[HookSpec, ...]] = field(default_factory=dict)

    # ------------------------------------------------------------------ states
    @property
    def initial_states(self) -> tuple[StateSpec, ...]:
        return tuple(state for state in self.ordered_states if state.is_initial)

    @property
    def terminal_states(self) -> tuple[StateSpec, ...]:
        return tuple(state for state in self.ordered_states if state.is_terminal)

    @property
    def ordered_states(self) -> tuple[StateSpec, ...]:
        return tuple(sorted(self.states.values(), key=lambda state: (state.order, state.pk)))

    def state(self, status_key: str) -> StateSpec | None:
        return self.states.get(status_key)

    def has_state(self, status_key: str) -> bool:
        return status_key in self.states

    # ------------------------------------------------------------- transitions
    def transitions_from(self, status_key: str | None) -> tuple[TransitionSpec, ...]:
        """Every edge leaving ``status_key``; pass ``None`` for the creation edges."""
        return self._by_source.get(status_key, ())

    def candidates(self, from_key: str | None, action: str) -> tuple[TransitionSpec, ...]:
        """Every edge leaving ``from_key`` under ``action``, in ``order``.

        More than one is legal: two states may be joined by several edges, and the
        engine picks the first whose guard and permission both hold.
        """
        return self._by_action.get((from_key, action), ())

    def find(self, from_key: str | None, action: str) -> TransitionSpec | None:
        """The first edge for ``(from_key, action)``, or ``None`` if there is none."""
        found = self.candidates(from_key, action)
        return found[0] if found else None

    def named(self, from_key: str | None, name: str) -> TransitionSpec | None:
        """The edge called ``name`` among those leaving ``from_key``."""
        return self._by_name.get((from_key, name))

    def actions_from(self, status_key: str | None) -> tuple[str, ...]:
        seen: list[str] = []
        for transition in self.transitions_from(status_key):
            if transition.action not in seen:
                seen.append(transition.action)
        return tuple(seen)

    def names_from(self, status_key: str | None) -> tuple[str, ...]:
        return tuple(transition.name for transition in self.transitions_from(status_key))

    # ------------------------------------------------------------------- hooks
    def hooks_for_transition(self, timing: str, transition_pk: int) -> tuple[HookSpec, ...]:
        specific = self._hooks.get((timing, HookEvent.TRANSITION, transition_pk), ())
        any_edge = self._hooks.get((timing, HookEvent.ANY_TRANSITION, None), ())
        if not any_edge:
            return specific
        if not specific:
            return any_edge
        return tuple(sorted((*specific, *any_edge), key=lambda hook: (hook.order, hook.pk)))

    def hooks_for_state(
        self, timing: str, event: str, status_key: str | None
    ) -> tuple[HookSpec, ...]:
        if status_key is None:
            return ()
        return self._hooks.get((timing, event, status_key), ())

    @property
    def handler_keys(self) -> tuple[str, ...]:
        seen = {hook.handler_key for hooks in self._hooks.values() for hook in hooks}
        return tuple(sorted(seen))


# ------------------------------------------------------------------------ building


def build_graph(version: StateMachineVersion) -> VersionGraph:
    """Read one version out of the database and freeze it."""
    machine = version.state_machine
    states_by_pk: dict[int, StateSpec] = {}
    states: dict[str, StateSpec] = {}
    for state in _related(version, "states", lambda: version.states.select_related("status")):
        spec = _state_spec(state)
        states_by_pk[state.pk] = spec
        states[spec.key] = spec

    transitions: list[TransitionSpec] = []
    by_source: dict[str | None, list[TransitionSpec]] = {}
    by_action: dict[tuple[str | None, str], list[TransitionSpec]] = {}
    by_name: dict[tuple[str | None, str], TransitionSpec] = {}
    edges = _related(
        version, "transitions", lambda: version.transitions.select_related("action_type")
    )
    for transition in edges:
        from_spec = (
            states_by_pk.get(transition.from_state_id) if transition.from_state_id else None
        )
        to_spec = states_by_pk.get(transition.to_state_id)
        if to_spec is None:
            # A transition pointing outside its own version; validation reports it.
            continue
        edge = TransitionSpec(
            pk=transition.pk,
            name=transition.name,
            from_key=from_spec.key if from_spec else CREATION,
            to_key=to_spec.key,
            action=transition.action_type.key,
            action_pk=transition.action_type_id,
            guard=transition.guard,
            required_permission=transition.required_permission,
            requires_approval=transition.requires_approval,
            order=transition.order,
            description=transition.description,
        )
        transitions.append(edge)
        by_source.setdefault(edge.from_key, []).append(edge)
        by_action.setdefault((edge.from_key, edge.action), []).append(edge)
        by_name[(edge.from_key, edge.name)] = edge

    hooks: dict[tuple[str, str, object], list[HookSpec]] = {}
    for hook in _related(version, "hooks", lambda: version.hooks.all()):
        if not hook.is_active:
            continue
        hook_spec = _hook_spec(hook, states_by_pk)
        target: object
        if hook.event == HookEvent.TRANSITION:
            target = hook.transition_id
        elif hook.event == HookEvent.ANY_TRANSITION:
            target = None
        else:
            target = hook_spec.state_key
        hooks.setdefault((hook.timing, hook.event, target), []).append(hook_spec)

    return VersionGraph(
        version_pk=version.pk,
        version_label=version.version,
        lifecycle=version.lifecycle,
        machine_key=machine.key,
        entity_type=machine.entity_type,
        status_field=machine.status_field,
        modified_at=version.modified_at,
        scope_pk=machine.scope_id,
        states=states,
        transitions=tuple(sorted(transitions, key=_edge_sort_key)),
        _by_source={
            key: tuple(sorted(value, key=_edge_sort_key)) for key, value in by_source.items()
        },
        _by_action={
            key: tuple(sorted(value, key=_edge_sort_key)) for key, value in by_action.items()
        },
        _by_name=by_name,
        _hooks={
            key: tuple(sorted(value, key=lambda hook: (hook.order, hook.pk)))
            for key, value in hooks.items()
        },
    )


def _edge_sort_key(edge: TransitionSpec) -> tuple[int, int]:
    """``order`` first, primary key as the tiebreak, so resolution is deterministic."""
    return (edge.order, edge.pk)


def _related(
    version: StateMachineVersion, name: str, fallback: Callable[[], Any]
) -> Iterable[Any]:
    """Reuse ``with_graph()``'s prefetch when it is there, and query only when it is not.

    ``version.states.select_related(...)`` builds a fresh queryset and would silently
    ignore a prefetch, which is exactly the N+1 the prefetch was meant to avoid.
    """
    cache = getattr(version, "_prefetched_objects_cache", None)
    if cache is not None and name in cache:
        return list(cache[name])
    rows: Iterable[Any] = fallback()
    return rows


def _state_spec(state: StateMachineState) -> StateSpec:
    return StateSpec(
        pk=state.pk,
        status_pk=state.status_id,
        key=state.status.key,
        name=state.status.name,
        is_initial=state.is_initial,
        is_terminal=state.is_terminal,
        color=state.color,
        order=state.order,
        x=state.x,
        y=state.y,
    )


def _hook_spec(hook: StateMachineHook, states_by_pk: dict[int, StateSpec]) -> HookSpec:
    state = states_by_pk.get(hook.state_id) if hook.state_id else None
    return HookSpec(
        pk=hook.pk,
        handler_key=hook.handler_key,
        timing=hook.timing,
        event=hook.event,
        transition_pk=hook.transition_id,
        state_key=state.key if state else None,
        params=dict(hook.params or {}),
        order=hook.order,
        on_commit=hook.on_commit,
    )


# ------------------------------------------------------------------------- caching

_cache: dict[int, VersionGraph] = {}
_lock = threading.RLock()


def get_graph(version: StateMachineVersion | int) -> VersionGraph:
    """Return the graph for ``version``, building and caching it on first use.

    Passing a loaded instance avoids a query: the cached entry is reused only when its
    ``modified_at`` still matches the instance's, so a stale graph can never be served.
    """
    from vinta_state_machines.models import StateMachineVersion

    resolved = (
        StateMachineVersion.objects.select_related("state_machine").get(pk=version)
        if isinstance(version, int)
        else version
    )
    if not get_setting("CACHE_GRAPHS"):
        return build_graph(resolved)

    with _lock:
        cached = _cache.get(resolved.pk)
        if cached is not None and cached.modified_at == resolved.modified_at:
            return cached
    graph = build_graph(resolved)
    with _lock:
        _cache[resolved.pk] = graph
    return graph


def invalidate_graph(version_pk: int) -> None:
    """Drop one version's cached graph."""
    with _lock:
        _cache.pop(version_pk, None)


def clear_graph_cache() -> None:
    """Drop every cached graph. Call this between tests that reuse primary keys."""
    with _lock:
        _cache.clear()


__all__ = [
    "CREATION",
    "HookEvent",
    "HookSpec",
    "HookTiming",
    "StateSpec",
    "TransitionSpec",
    "VersionGraph",
    "build_graph",
    "clear_graph_cache",
    "get_graph",
    "invalidate_graph",
]
