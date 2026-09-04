"""Side effects the tests assert on, registered the way a real app would."""

from __future__ import annotations

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


SINKED: list[Any] = []


def collect_runs(runs: list[Any]) -> None:
    """A ``SIDE_EFFECT_RUN_SINK`` that keeps the rows in memory instead of writing them."""
    SINKED.extend(runs)


def reset() -> None:
    CALLS.clear()
    SINKED.clear()
