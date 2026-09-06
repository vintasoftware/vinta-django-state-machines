"""Side effects the tests assert on, registered the way a real app would."""

from __future__ import annotations

import asyncio
from typing import Any

from vinta_state_machines.side_effects import AbortTransition, register_side_effect

CALLS: list[dict[str, Any]] = []


@register_side_effect("testapp.record")
def record(context: Any) -> None:
    """Append a trace entry, so tests can assert on order and payload."""
    CALLS.append(
        {
            "handler": "testapp.record",
            "timing": context.timing,
            "event": context.event,
            "from": context.from_status,
            "to": context.to_status,
            "action": context.action,
            "label": context.params.get("label", ""),
            "instance": context.instance,
            "record": context.record,
            "params": dict(context.params),
            "metadata": dict(context.metadata),
        }
    )


@register_side_effect("testapp.bump_amount")
def bump_amount(context: Any) -> None:
    context.instance.amount += context.params.get("by", 1)
    context.touch("amount")


@register_side_effect("testapp.bump_amount_without_touching")
def bump_amount_without_touching(context: Any) -> None:
    context.instance.amount += context.params.get("by", 1)


@register_side_effect("testapp.veto")
def veto(context: Any) -> None:
    raise AbortTransition(context.params.get("reason", "vetoed by testapp.veto"))


@register_side_effect("testapp.boom")
def boom(context: Any) -> None:
    raise RuntimeError("boom")


# ----------------------------------------------------------------------- async

# Deliberate mirrors of the synchronous handlers above, so a test can assert that the
# two kinds behave identically rather than merely that the async ones run at all.


@register_side_effect("testapp.arecord")
async def arecord(context: Any) -> None:
    """Append a trace entry from a coroutine, noting the loop it was awaited on."""
    await asyncio.sleep(0)
    CALLS.append(
        {
            "handler": "testapp.arecord",
            "timing": context.timing,
            "event": context.event,
            "from": context.from_status,
            "to": context.to_status,
            "action": context.action,
            "label": context.params.get("label", ""),
            "instance": context.instance,
            "record": context.record,
            "params": dict(context.params),
            "metadata": dict(context.metadata),
            "loop_id": id(asyncio.get_running_loop()),
        }
    )


@register_side_effect("testapp.abump_amount")
async def abump_amount(context: Any) -> None:
    await asyncio.sleep(0)
    context.instance.amount += context.params.get("by", 1)
    context.touch("amount")


@register_side_effect("testapp.aveto")
async def aveto(context: Any) -> None:
    await asyncio.sleep(0)
    raise AbortTransition(context.params.get("reason", "vetoed by testapp.aveto"))


@register_side_effect("testapp.aboom")
async def aboom(context: Any) -> None:
    await asyncio.sleep(0)
    raise RuntimeError("async boom")


class AsyncCallable:
    """A handler that is an *object* with ``async def __call__``, not a function.

    The shape a handler takes when it needs constructor arguments, and the one neither
    ``asyncio.iscoroutinefunction`` nor asgiref's recognises on the instance itself.
    """

    def __init__(self, tag: str) -> None:
        self.tag = tag

    async def __call__(self, context: Any) -> None:
        await asyncio.sleep(0)
        CALLS.append({"handler": self.tag, "timing": context.timing, "event": context.event})


register_side_effect("testapp.acallable")(AsyncCallable("testapp.acallable"))


def sync_decorator(func: Any) -> Any:
    """A plain ``def`` wrapper around a handler, the shape a retry decorator takes.

    ``functools.wraps`` does not carry the coroutine marker, so the result inspects as
    synchronous while still returning a coroutine -- the case the engine has to finish
    rather than drop.
    """
    import functools

    @functools.wraps(func)
    def wrapper(context: Any) -> Any:
        return func(context)

    return wrapper


register_side_effect("testapp.awrapped")(sync_decorator(arecord))


SINKED: list[Any] = []


def collect_runs(runs: list[Any]) -> None:
    """A ``SIDE_EFFECT_RUN_SINK`` that keeps the rows in memory instead of writing them."""
    SINKED.extend(runs)


def reset() -> None:
    CALLS.clear()
    SINKED.clear()
