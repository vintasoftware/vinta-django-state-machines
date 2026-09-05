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

A handler may be ``async def``, and is wired up exactly like any other::

    @register_side_effect("risk.notify_owner")
    async def notify_owner(context):
        await httpx_client.post(WEBHOOK, json={"to": context.to_status})

Async and sync handlers are interchangeable: the same key, the same
:class:`StateMachineHook` row, the same ordering, the same veto.  What differs is only
*where the coroutine is driven*, and that follows the caller.  Under
:func:`~vinta_state_machines.engine.atransition` the handler is awaited on the caller's
own event loop, so it shares the loop with the rest of the request.  Under the
synchronous :func:`~vinta_state_machines.engine.transition` there is no loop to borrow,
so one is created for the call and thrown away after -- correct, but a cost per handler,
which is the reason to prefer ``atransition`` from async code.

Either way the handler runs *inside the transition's transaction*, in handler order,
and a ``before`` handler that raises :class:`AbortTransition` still vetoes the move.
That is the point of driving the coroutine to completion rather than scheduling it: an
awaited handler is a handler whose failure can still roll the move back.  It is also the
caveat -- an ``async def`` handler holds an open database transaction for as long as it
is awaiting, so a slow call belongs on an ``on_commit`` binding, where it runs past the
commit, exactly as a slow synchronous handler would.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import TYPE_CHECKING, Any

from asgiref.sync import async_to_sync, iscoroutinefunction
from django.apps import apps
from django.db import models, transaction
from django.utils.module_loading import module_has_submodule

from vinta_state_machines.exceptions import StateMachineError
from vinta_state_machines.instruments import SideEffectEvent, current_span, observe
from vinta_state_machines.registry import Registry

if TYPE_CHECKING:
    from vinta_state_machines.graph import HookSpec, TransitionSpec, VersionGraph
    from vinta_state_machines.instruments import TransitionEvent
    from vinta_state_machines.models import StateMachineVersion, StatusTransition

SideEffect = Callable[["SideEffectContext"], Any]
"""A handler: ``def handler(context)`` or ``async def handler(context)``.

Both spellings share one type because both are called the same way and both return
whatever they return.  :func:`is_async_handler` is what tells them apart at the one
place it matters.
"""

side_effect_registry: Registry[SideEffect] = Registry(kind="side effect")
"""The process-wide registry of side-effect handlers."""


def is_async_handler(handler: SideEffect) -> bool:
    """Whether ``handler`` has to be awaited.

    ``iscoroutinefunction`` is asgiref's rather than ``asyncio``'s, so a handler wrapped
    in ``functools.partial`` or marked with ``markcoroutinefunction`` answers correctly.
    The second check covers a *callable object* with an ``async def __call__``, which is
    how a handler that needs constructor arguments is usually written and which neither
    implementation recognises on the instance itself.
    """
    return bool(iscoroutinefunction(handler)) or bool(
        iscoroutinefunction(getattr(handler, "__call__", None))  # noqa: B004
    )


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
    is_async: bool = False
    """The handler is ``async def``.  Descriptive here like everything else on this
    class -- the engine does not consult it -- but worth surfacing in an authoring UI,
    because it is what tells an author that this binding will hold the transaction open
    across an ``await`` unless it is put on ``on_commit``."""


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
            is_async=is_async_handler(func),
        )
        return registered

    return decorator


def get_side_effect(key: str) -> SideEffect:
    """Return the handler registered under ``key``, or raise ``NotRegistered``."""
    return side_effect_registry.get(key)


def is_async_side_effect(key: str) -> bool:
    """Whether the handler registered under ``key`` is ``async def``."""
    return is_async_handler(side_effect_registry.get(key))


def registered_side_effects() -> list[str]:
    """Every registered handler key, sorted."""
    return side_effect_registry.keys()


def side_effect_catalog() -> list[SideEffectInfo]:
    """Every registered handler with its presentation metadata, sorted by key.

    A handler registered straight through the registry rather than through
    :func:`register_side_effect` still appears, described by its key alone.
    """
    return [
        _side_effect_info.get(key)
        or SideEffectInfo(
            key=key, name=key, is_async=is_async_handler(side_effect_registry.get(key))
        )
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

    span_id: str = ""
    """Id of the observation this handler is running inside, or ``""`` if nothing is
    watching.  Log it and a handler's own lines join up with the move that caused them,
    without a thread-local of its own.  See :mod:`vinta_state_machines.instruments`."""

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
    transition_event: TransitionEvent | None = None,
) -> None:
    """Execute ``hooks`` in order, building each handler's context lazily.

    ``on_commit`` hooks are deferred to the end of the surrounding transaction; every
    other hook runs inline so that a ``before`` handler can still veto the change.

    ``recorder`` is a :class:`~vinta_state_machines.runs.RunRecorder`, which the engine
    supplies and nothing else has to.  An inline handler is timed into its buffer and
    written when the transition resolves; a deferred one records itself, since by the
    time it runs the buffer has long been flushed.  Left ``None``, nothing is recorded
    and the handlers run exactly as they always did.

    ``transition_event`` is the move these hooks belong to.  When it is there, each
    handler additionally gets a span of its own, nested under the transition's -- the
    same measurement the recorder is taking, kept for a different reader.  See
    :mod:`vinta_state_machines.instruments`.

    An ``async def`` handler is driven to completion by :func:`call_handler` rather than
    scheduled, so from here -- and from the recorder and the instruments bracketing it
    -- it is indistinguishable from a synchronous one: it returns, or it raises, before
    the next hook is reached.
    """
    for hook in hooks:
        handler = get_side_effect(hook.handler_key)
        context = context_factory(hook)
        if hook.on_commit and hook.timing == "after":
            transaction.on_commit(_bind(handler, context, recorder, record, transition_event))
            continue
        with ExitStack() as stack:
            if transition_event is not None:
                stack.enter_context(observe(_event_for(context, transition_event, deferred=False)))
            if recorder is not None:
                stack.enter_context(recorder.measure(hook, context.timing, context.event))
            call_handler(handler, context)


def call_handler(handler: SideEffect, context: SideEffectContext) -> Any:
    """Run one handler and return its result, awaiting it if it is a coroutine function.

    Deliberately *blocking* for both kinds.  Everything wrapped around this call --
    ``recorder.measure``, the handler's span, the enclosing ``atomic()`` block, and the
    ``before`` timing's power to veto -- assumes the handler has finished by the time
    the call returns.  Handing back an un-awaited coroutine would time nothing, record
    nothing, and turn ``AbortTransition`` into an unraisable warning at garbage
    collection instead of a rolled back move.

    ``async_to_sync`` is what does the driving, and which loop it borrows depends on the
    caller.  From :func:`~vinta_state_machines.engine.atransition` the engine's body is
    already inside ``sync_to_async(thread_sensitive=True)``, so asgiref hands the
    coroutine back to the caller's own running loop.  From the synchronous
    :func:`~vinta_state_machines.engine.transition` there is no loop to hand it to, so
    asgiref makes one for the call.
    """
    if is_async_handler(handler):
        return _drive(handler, context)

    result = handler(context)
    # A *synchronous* callable can still hand back a coroutine: a plain ``def``
    # decorator wrapped around an async handler is the usual way it happens, and
    # ``functools.wraps`` does not carry the coroutine marker across, so
    # :func:`is_async_handler` cannot see through it. Dropping that return value would
    # run none of the handler and report success, so it is finished here rather than
    # left to garbage collection and an unraisable warning.
    if not isawaitable(result):
        return result
    try:
        return _drive(_finishing(result), context)
    except BaseException:
        # ``_drive`` can refuse before it awaits anything, and an abandoned coroutine
        # warns at collection. Close it so the caller sees only the real error.
        close = getattr(result, "close", None)
        if close is not None:
            close()
        raise


def _finishing(pending: Any) -> SideEffect:
    """Wrap an awaitable somebody else already made, so :func:`_drive` can take it.

    ``async_to_sync`` insists on a coroutine *function* and will not accept a plain
    callable that happens to return a coroutine, which is exactly what the wrapped
    handler above is.
    """

    async def finish(context: SideEffectContext) -> Any:
        return await pending

    return finish


def _drive(handler: SideEffect, context: SideEffectContext) -> Any:
    """Run ``handler`` to completion on a loop, translating asgiref's one refusal."""
    try:
        return async_to_sync(handler)(context)
    except RuntimeError as exc:
        # Reached when synchronous ``transition()`` is called from a thread that is
        # already running an event loop: asgiref cannot start a second one there. The
        # fix is a different entry point, so say which one rather than leaving asgiref's
        # generic message to be traced back to a hook nobody was thinking about.
        if "AsyncToSync" not in str(exc):
            raise
        raise RuntimeError(
            f"The side effect {context.hook.handler_key!r} is async, and transition() was "
            "called from a thread that already runs an event loop, so it cannot be "
            "awaited here. Use `await atransition(...)` from async code, or move the "
            "call off the loop with sync_to_async."
        ) from exc


def _event_for(
    context: SideEffectContext, transition_event: TransitionEvent, *, deferred: bool
) -> SideEffectEvent:
    """Describe one handler run, hung off the move it belongs to."""
    return SideEffectEvent(
        handler_key=context.hook.handler_key,
        hook_pk=context.hook.pk,
        timing=context.timing,
        event=context.event,
        deferred=deferred,
        transition=transition_event,
    )


def _bind(
    handler: SideEffect,
    context: SideEffectContext,
    recorder: Any,
    record: Any,
    transition_event: TransitionEvent | None = None,
) -> Callable[[], Any]:
    # Captured now, not when the closure runs. A deferred handler runs after the
    # transition's ``with`` block has closed, so by then the ambient span is gone and
    # the parent has to be carried explicitly for the trace to keep its shape.
    parent_span = current_span()
    parent_id = parent_span.id if parent_span is not None else None

    def run() -> Any:
        with ExitStack() as stack:
            if transition_event is not None:
                stack.enter_context(
                    observe(
                        _event_for(context, transition_event, deferred=True),
                        parent_id=parent_id,
                    )
                )
            if recorder is not None:
                from vinta_state_machines.runs import record_deferred_run

                stack.enter_context(
                    record_deferred_run(
                        recorder, context.hook, context.timing, context.event, record
                    )
                )
            return call_handler(handler, context)

    return run
