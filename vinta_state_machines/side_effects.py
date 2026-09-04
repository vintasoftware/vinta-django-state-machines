"""Side effects: registered functions that fire around a status change.

Any app can register a handler under a stable key::

    # myapp/side_effects.py
    from vinta_state_machines.side_effects import register_side_effect


    @register_side_effect("risk.notify_owner")
    def notify_owner(context):
        send_mail_to(context.instance.owner, context.to_status, **context.params)

A :class:`~vinta_state_machines.models.StateMachineHook` row then references that key
and says *when* it runs: before or after the change is committed, and whether it is
bound to a specific transition, to any transition of the version, or to entering or
leaving a given state.  That row also carries a JSON ``params`` parameter, so the same
handler can be wired to several transitions and behave differently on each.

Handlers receive a single :class:`SideEffectContext` argument.  A ``before`` handler may
abort the whole transition by raising :class:`AbortTransition`; the status change and
every remaining handler are skipped and nothing is written.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.apps import apps
from django.db import models, transaction
from django.utils.module_loading import module_has_submodule

from vinta_state_machines.exceptions import StateMachineError
from vinta_state_machines.registry import Registry

if TYPE_CHECKING:
    from vinta_state_machines.graph import HookSpec, TransitionSpec, VersionGraph
    from vinta_state_machines.models import StateMachineVersion, StatusTransition

SideEffect = Callable[["SideEffectContext"], Any]

side_effect_registry: Registry[SideEffect] = Registry(kind="side effect")
"""The process-wide registry of side-effect handlers."""


class AbortTransition(StateMachineError):
    """Raised by a ``before`` handler to veto a transition."""

    default_code = "transition_aborted"


@dataclass(frozen=True)
class SideEffectInfo:
    """What a handler calls itself, for the people wiring it up.

    Purely descriptive: nothing here changes how the handler runs.  It exists so an
    authoring UI can offer a catalog of readable names instead of bare keys.
    """

    key: str
    name: str
    description: str = ""
    default_params: dict[str, Any] = field(default_factory=dict)


_side_effect_info: dict[str, SideEffectInfo] = {}


def register_side_effect(
    key: str,
    *,
    replace: bool = False,
    name: str = "",
    description: str = "",
    default_params: dict[str, Any] | None = None,
) -> Callable[[SideEffect], SideEffect]:
    """Register a side-effect handler under a stable, unique ``key``.

    Usable as a decorator or called directly with the function.  ``name``,
    ``description`` and ``default_params`` are optional presentation metadata for
    authoring UIs; the name falls back to the key and the description to the
    handler's own docstring.
    """

    def decorator(func: SideEffect) -> SideEffect:
        registered = side_effect_registry.register(key, func, replace=replace)
        _side_effect_info[key] = SideEffectInfo(
            key=key,
            name=name or key,
            description=description or (func.__doc__ or "").strip().split("\n\n")[0],
            default_params=dict(default_params or {}),
        )
        return registered

    return decorator


def get_side_effect(key: str) -> SideEffect:
    """Return the handler registered under ``key``, or raise ``NotRegistered``."""
    return side_effect_registry.get(key)


def registered_side_effects() -> list[str]:
    """Every registered handler key, sorted."""
    return side_effect_registry.keys()


def side_effect_catalog() -> list[SideEffectInfo]:
    """Every registered handler with its presentation metadata, sorted by key.

    A handler registered straight through the registry rather than through
    :func:`register_side_effect` still appears, described by its key alone.
    """
    return [
        _side_effect_info.get(key) or SideEffectInfo(key=key, name=key)
        for key in side_effect_registry
    ]


def autodiscover() -> None:
    """Import ``side_effects`` from every installed app, so handlers register once."""
    from importlib import import_module

    for config in apps.get_app_configs():
        if config.module is not None and module_has_submodule(config.module, "side_effects"):
            import_module(f"{config.name}.side_effects")


@dataclass(frozen=True)
class SideEffectContext:
    """Everything a handler needs to know about the change it is reacting to."""

    instance: models.Model
    """The record whose status is changing."""

    field_name: str
    """Name of the status field on ``instance`` that is changing."""

    status_field: str
    """The catalog's ``status_field`` this machine governs."""

    from_status: str | None
    """Status key the record is leaving, or ``None`` on creation."""

    to_status: str
    """Status key the record is moving to."""

    action: str
    """Key of the :class:`ActionType` driving the change."""

    version: StateMachineVersion
    """The version that authorized the edge."""

    graph: VersionGraph
    """The in-memory graph of ``version``."""

    transition: TransitionSpec
    """The edge being taken."""

    timing: str
    """``"before"`` or ``"after"``."""

    event: str
    """Which hook binding fired: ``transition``, ``enter_state``, ``leave_state``, ..."""

    hook: HookSpec
    """The hook row that wired this handler in."""

    actor: Any = None
    """The *live* principal that triggered the move, exactly as the caller passed it.

    A user, an identity row, an ``IdentitySnapshot``, or ``None`` for the system. This
    is deliberately not the identity row the history was written against: ``before``
    handlers run before that row exists, and a handler that wants to check a permission
    needs something it can ask. ``after`` handlers that want the recorded snapshot read
    it from ``context.record.actor``.
    """

    params: dict[str, Any] = field(default_factory=dict)
    """The JSON parameter stored on this handler's binding, verbatim.

    This is authoring-time configuration that travels with the version — the same
    handler wired to two transitions can be given different parameters on each. For
    per-call data supplied by whoever triggered the move, see :attr:`metadata`.
    """

    metadata: dict[str, Any] = field(default_factory=dict)
    """Free-form data passed into ``transition()`` by the caller, at the time of the move."""

    record: StatusTransition | None = None
    """The history row. Only set for ``after`` handlers, and ``None`` if history is off."""

    touched: set[str] = field(default_factory=set, repr=False, compare=False)
    """Extra fields of ``instance`` the engine should persist. See :meth:`touch`."""

    def touch(self, *field_names: str) -> None:
        """Ask the engine to persist these extra fields of ``instance``.

        The engine saves only the status columns, so a handler that also changes the
        record has to say so::

            @register_side_effect("risk.stamp_closed_at")
            def stamp_closed_at(context):
                context.instance.closed_at = timezone.now()
                context.touch("closed_at")

        Fields touched by a ``before`` handler ride along with the status write; fields
        touched by an ``after`` handler are written straight afterwards, in the same
        transaction.
        """
        self.touched.update(field_names)


def run_hooks(
    hooks: list[HookSpec],
    context_factory: Callable[[HookSpec], SideEffectContext],
    *,
    recorder: Any = None,
    record: Any = None,
) -> None:
    """Execute ``hooks`` in order, building each handler's context lazily.

    ``on_commit`` hooks are deferred to the end of the surrounding transaction; every
    other hook runs inline so that a ``before`` handler can still veto the change.

    ``recorder`` is a :class:`~vinta_state_machines.runs.RunRecorder`, which the engine
    supplies and nothing else has to.  An inline handler is timed into its buffer and
    written when the transition resolves; a deferred one records itself, since by the
    time it runs the buffer has long been flushed.  Left ``None``, nothing is recorded
    and the handlers run exactly as they always did.
    """
    for hook in hooks:
        handler = get_side_effect(hook.handler_key)
        context = context_factory(hook)
        if hook.on_commit and hook.timing == "after":
            transaction.on_commit(_bind(handler, context, recorder, record))
        elif recorder is None:
            handler(context)
        else:
            with recorder.measure(hook, context.timing, context.event):
                handler(context)


def _bind(
    handler: SideEffect, context: SideEffectContext, recorder: Any, record: Any
) -> Callable[[], Any]:
    def run() -> Any:
        if recorder is None:
            return handler(context)
        from vinta_state_machines.runs import record_deferred_run

        with record_deferred_run(recorder, context.hook, context.timing, context.event, record):
            return handler(context)

    return run
