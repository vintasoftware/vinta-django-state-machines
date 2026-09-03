"""The side effects that wire a batch into a graph, and the helper that writes them.

Two handlers, registered by the app itself so any version can reference them:

``state_machines.open_batch``
    Bound to entering the state that fans work out.  Opens the batch; your own handler
    then creates the children and hands them to whatever runs them.

``state_machines.report_to_batch``
    Bound to *both* events of every finished state.  Arriving counts the child; leaving
    un-counts it.  One registered function, two bindings, so the pair is obvious in a
    hook list and impossible to half-implement through the helper below.

A "finished" state is one wired with this handler.  Whether it is also flagged
``is_terminal`` is a separate modelling choice, and it is the one that decides whether
un-counting can ever happen: the engine refuses to leave a terminal state, so a
``leave_state`` binding on one could never fire.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.utils.dateparse import parse_duration

from vinta_state_machines.batches import SUCCESS, count_child, open_batch, uncount_child
from vinta_state_machines.enums import HookEvent, HookTiming
from vinta_state_machines.side_effects import register_side_effect

if TYPE_CHECKING:
    from datetime import timedelta

    from vinta_state_machines.models import StateMachineHook, StateMachineVersion
    from vinta_state_machines.side_effects import SideEffectContext

REPORT_HANDLER = "state_machines.report_to_batch"
OPEN_HANDLER = "state_machines.open_batch"

__all__ = ["OPEN_HANDLER", "REPORT_HANDLER", "wire_batch_reporting"]


@register_side_effect(
    OPEN_HANDLER,
    name="Open a fan-out batch",
    description="Start counting the children this record is about to wait on.",
    default_params={"join_action": ""},
)
def open_batch_effect(context: SideEffectContext) -> None:
    """Open the batch as the record enters a state that fans work out.

    Safe to fire twice: at most one live batch per record per status field is a database
    constraint, so a retried transition finds the batch it already made.
    """
    open_batch(
        context.instance,
        join_action=context.params["join_action"],
        field_name=context.field_name,
        total=context.params.get("total"),
        timeout=_duration(context.params.get("timeout")),
        join_retry_after=_duration(context.params.get("join_retry_after")),
        actor=context.actor,
    )


@register_side_effect(
    REPORT_HANDLER,
    name="Report to the batch counting this record",
    description="Count this record when it finishes, and un-count it if it un-finishes.",
    default_params={"outcome": SUCCESS},
)
def report_to_batch(context: SideEffectContext) -> None:
    """Count on the way in, un-count on the way out.

    The two directions are the same handler on purpose.  They are one concept stored as
    two rows, and reading ``context.event`` keeps them from drifting apart.
    """
    outcome = context.params.get("outcome", SUCCESS)
    batch_field = context.params.get("batch_field")

    if context.event == HookEvent.ENTER_STATE:
        count_child(context.instance, outcome=outcome, batch_field=batch_field)
    elif context.event == HookEvent.LEAVE_STATE:
        uncount_child(context.instance, outcome=outcome, batch_field=batch_field)


def _duration(value: Any) -> timedelta | None:
    """Accept an ISO 8601 string from a JSON param, or a timedelta from Python."""
    if value is None or isinstance(value, type(None)):
        return None
    if isinstance(value, str):
        return parse_duration(value)
    parsed: timedelta = value
    return parsed


# ------------------------------------------------------------------- wiring


def wire_batch_reporting(
    version: StateMachineVersion,
    outcomes: dict[str, str],
    *,
    batch_field: str | None = None,
    on_commit: bool = True,
) -> list[StateMachineHook]:
    """Wire every finished state of ``version`` to report to its batch.

    ``outcomes`` maps a status key to ``"success"`` or ``"failure"``::

        wire_batch_reporting(row_v1, {"processed": "success", "rejected": "failure"})

    Writes the ``leave_state`` half **only where the state can actually be left**.  On a
    state flagged terminal the engine refuses the move, so that binding could never fire
    and there is nothing to pair; the helper writes the one row that can and says
    nothing about it.

    This exists so the pair is never half-written by hand.  ``validate_version`` treats a
    lone ``enter_state`` binding on a reopenable state as an error, because a child that
    can come back has to be able to bring the counter back with it.

    ``on_commit`` defaults to true, and not only out of habit: every child touching one
    counter row means the batch is the contended row in the whole fan-out.  Deferring
    holds its lock for the length of one UPDATE rather than for the length of whatever
    transaction the child's own work happens to be sitting in.
    """
    from vinta_state_machines.models import StateMachineHook

    created: list[StateMachineHook] = []
    params: dict[str, Any] = {}
    if batch_field is not None:
        params["batch_field"] = batch_field

    for status_key, outcome in outcomes.items():
        state = version.states.select_related("status").get(status__key=status_key)
        events = [HookEvent.ENTER_STATE]
        if not state.is_terminal:
            events.append(HookEvent.LEAVE_STATE)
        for event in events:
            hook, _ = StateMachineHook.objects.get_or_create(
                state_machine_version=version,
                handler_key=REPORT_HANDLER,
                event=event,
                state=state,
                defaults={
                    "timing": HookTiming.AFTER,
                    "on_commit": on_commit,
                    "params": {"outcome": outcome, **params},
                },
            )
            created.append(hook)
    return created
