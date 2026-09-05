"""Async side effects, and the engine's async twins.

Two entry points reach the same code, and both are covered here:

* ``async_to_sync(atransition)(...)`` from a synchronous test.  asgiref sends the
  thread-sensitive body back to the *calling* thread, so the move runs on the test's own
  connection and the ordinary ``django_db`` fixture is enough.  Most tests use this.
* A real coroutine under ``pytest.mark.asyncio``.  There the body runs on an executor
  thread with a connection of its own, so those tests take ``transactional_db`` and are
  marked as such -- they are the ones that prove the production path rather than a
  convenient stand-in for it.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import transaction as db_transaction

from tests.testapp import side_effects
from tests.testapp.models import Risk
from vinta_state_machines.editor import side_effect_definitions
from vinta_state_machines.engine import (
    aavailable_actions,
    aavailable_transitions,
    acan_transition,
    acurrent_state,
    agraph_for,
    ainitial_status_key,
    aresolve_version,
    atransition,
    transition,
)
from vinta_state_machines.models import SideEffectRun, StateMachineHook, StatusTransition
from vinta_state_machines.side_effects import (
    AbortTransition,
    is_async_handler,
    is_async_side_effect,
    side_effect_catalog,
)

pytestmark = pytest.mark.django_db


def hook(version, handler="testapp.arecord", **kwargs):
    """Attach a hook to a version, resolving the state or transition by key."""
    state_key = kwargs.pop("state", None)
    edge = kwargs.pop("transition", None)
    if state_key:
        kwargs["state"] = version.states.get(status__key=state_key)
    if edge:
        kwargs["transition"] = version.transitions.get(name=edge)
    return StateMachineHook.objects.create(
        state_machine_version=version, handler_key=handler, **kwargs
    )


def traced(field="event"):
    return [call[field] for call in side_effects.CALLS]


def move(record, action, **kwargs):
    """Run ``atransition`` from a synchronous test, on the test's own thread."""
    return async_to_sync(atransition)(record, action, **kwargs)


# ------------------------------------------------------------------ detection


def test_a_coroutine_handler_is_recognised():
    assert is_async_side_effect("testapp.arecord") is True
    assert is_async_side_effect("testapp.record") is False


def test_a_callable_object_with_an_async_call_is_recognised():
    assert is_async_side_effect("testapp.acallable") is True


def test_a_partial_of_a_coroutine_is_recognised():
    import functools

    async def handler(context): ...

    assert is_async_handler(functools.partial(handler)) is True


def test_the_catalog_says_which_handlers_are_async():
    catalog = {info.key: info.is_async for info in side_effect_catalog()}
    assert catalog["testapp.arecord"] is True
    assert catalog["testapp.record"] is False


def test_the_editor_payload_carries_the_async_flag():
    payload = {entry["id"]: entry["isAsync"] for entry in side_effect_definitions()}
    assert payload["testapp.arecord"] is True
    assert payload["testapp.record"] is False


# --------------------------------------------------------------------- firing


def test_an_async_after_hook_runs(risk_version, risk):
    hook(risk_version, timing="after", event="transition", transition="assess")
    move(risk, "risk.assess")
    assert traced("handler") == ["testapp.arecord"]
    assert traced("to") == ["assessed"]


def test_an_async_hook_runs_under_the_synchronous_engine_too(risk_version, risk):
    """No loop to borrow, so asgiref makes one. The handler must not notice."""
    hook(risk_version, timing="after", event="transition", transition="assess")
    transition(risk, "risk.assess")
    assert traced("handler") == ["testapp.arecord"]
    risk.refresh_from_db()
    assert risk.status_key == "assessed"


def test_async_and_sync_hooks_interleave_in_declared_order(risk_version, risk):
    hook(
        risk_version,
        "testapp.record",
        timing="after",
        event="transition",
        transition="assess",
        order=0,
        params={"label": "sync"},
    )
    hook(
        risk_version,
        "testapp.arecord",
        timing="after",
        event="transition",
        transition="assess",
        order=1,
        params={"label": "async"},
    )
    hook(
        risk_version,
        "testapp.record",
        timing="after",
        event="transition",
        transition="assess",
        order=2,
        params={"label": "sync-again"},
    )
    move(risk, "risk.assess")
    assert traced("label") == ["sync", "async", "sync-again"]


def test_a_callable_object_handler_runs(risk_version, risk):
    hook(
        risk_version, "testapp.acallable", timing="after", event="transition", transition="assess"
    )
    move(risk, "risk.assess")
    assert traced("handler") == ["testapp.acallable"]


def test_an_async_hook_sees_the_history_row(risk_version, risk):
    hook(risk_version, timing="after", event="transition", transition="assess")
    record = move(risk, "risk.assess")
    assert side_effects.CALLS[0]["record"] == record


def test_an_async_hook_receives_params_and_metadata(risk_version, risk):
    hook(
        risk_version,
        timing="after",
        event="transition",
        transition="assess",
        params={"label": "audit"},
    )
    move(risk, "risk.assess", metadata={"ticket": "OPS-1"})
    assert side_effects.CALLS[0]["params"] == {"label": "audit"}
    assert side_effects.CALLS[0]["metadata"] == {"ticket": "OPS-1"}


def test_an_async_enter_state_hook_fires(risk_version, risk):
    hook(risk_version, timing="after", event="enter_state", state="assessed")
    move(risk, "risk.assess")
    assert traced("event") == ["enter_state"]


# ----------------------------------------------------------------- the transaction


def test_an_async_before_hook_can_veto_the_move(risk_version, risk):
    hook(risk_version, "testapp.aveto", timing="before", event="transition", transition="assess")
    with pytest.raises(AbortTransition, match="vetoed by testapp.aveto"):
        move(risk, "risk.assess")
    risk.refresh_from_db()
    assert risk.status_key == "draft"
    assert not StatusTransition.objects.exists()


def test_an_async_hook_that_raises_rolls_the_move_back(risk_version, risk):
    hook(risk_version, "testapp.aboom", timing="after", event="transition", transition="assess")
    with pytest.raises(RuntimeError, match="async boom"):
        move(risk, "risk.assess")
    risk.refresh_from_db()
    assert risk.status_key == "draft"
    assert not StatusTransition.objects.exists()


def test_an_async_before_hook_can_touch_fields(risk_version, risk):
    hook(
        risk_version,
        "testapp.abump_amount",
        timing="before",
        event="transition",
        transition="assess",
        params={"by": 25},
    )
    move(risk, "risk.assess")
    risk.refresh_from_db()
    assert risk.amount == 525
    assert risk.status_key == "assessed"


def test_an_async_after_hook_can_touch_fields(risk_version, risk):
    hook(
        risk_version,
        "testapp.abump_amount",
        timing="after",
        event="transition",
        transition="assess",
        params={"by": 7},
    )
    move(risk, "risk.assess")
    risk.refresh_from_db()
    assert risk.amount == 507


def test_an_async_on_commit_hook_waits_for_the_transaction(risk_version, risk):
    hook(risk_version, timing="after", event="transition", transition="assess", on_commit=True)
    with db_transaction.atomic():
        move(risk, "risk.assess")
        assert traced("handler") == []


def test_an_async_on_commit_hook_runs_when_the_transaction_commits(
    risk_version, risk, django_capture_on_commit_callbacks
):
    """The binding a slow ``async def`` handler belongs on: past the commit."""
    hook(risk_version, timing="after", event="transition", transition="assess", on_commit=True)
    with django_capture_on_commit_callbacks(execute=True):
        move(risk, "risk.assess")
    assert traced("handler") == ["testapp.arecord"]


# ------------------------------------------------------------------- recording


def test_a_failing_async_hook_is_recorded_like_a_failing_sync_one(risk_version, risk):
    binding = hook(
        risk_version, "testapp.aboom", timing="after", event="transition", transition="assess"
    )
    with pytest.raises(RuntimeError, match="async boom"):
        move(risk, "risk.assess")
    run = SideEffectRun.objects.get()
    assert run.hook_id == binding.pk
    assert run.outcome == "failed"
    assert run.error_class == "builtins.RuntimeError"
    # Timed, not skipped: the coroutine was driven to completion inside the brackets.
    assert run.duration_ms is not None


def test_a_successful_async_hook_is_timed(risk_version, risk, settings):
    settings.STATE_MACHINES = {"CACHE_GRAPHS": False, "RECORD_SIDE_EFFECT_RUNS": "all"}
    hook(risk_version, timing="after", event="transition", transition="assess")
    move(risk, "risk.assess")
    run = SideEffectRun.objects.get()
    assert run.outcome == "succeeded"
    assert run.handler_key == "testapp.arecord"


# ---------------------------------------------------------- instrumentation


def test_an_async_hook_gets_its_own_span(risk_version, risk, settings):
    from vinta_state_machines.instruments import SideEffectEvent, observation_finished

    seen = []

    def watch(sender, span, **kwargs):
        if sender is SideEffectEvent:
            seen.append((span.event.handler_key, span.outcome))

    settings.STATE_MACHINES = {
        "CACHE_GRAPHS": False,
        "INSTRUMENTS": ["vinta_state_machines.instruments.SignalInstrument"],
        "INSTRUMENT_STRICT": True,
    }
    observation_finished.connect(watch)
    try:
        hook(risk_version, timing="after", event="transition", transition="assess")
        move(risk, "risk.assess")
    finally:
        observation_finished.disconnect(watch)
    assert seen == [("testapp.arecord", "ok")]


# ------------------------------------------------------ handlers that hide async


def test_a_sync_wrapper_returning_a_coroutine_is_still_finished(risk_version, risk):
    """``functools.wraps`` hides the marker, so detection has to fall back to the result."""
    assert is_async_side_effect("testapp.awrapped") is False
    hook(risk_version, "testapp.awrapped", timing="after", event="transition", transition="assess")
    move(risk, "risk.assess")
    assert traced("handler") == ["testapp.arecord"]


def test_a_wrapped_async_veto_still_rolls_the_move_back(risk_version, risk):
    """The reason dropping the coroutine would not do: the veto is inside it."""
    from tests.testapp.side_effects import aveto, sync_decorator
    from vinta_state_machines.side_effects import register_side_effect, side_effect_registry

    register_side_effect("testapp.awrapped_veto")(sync_decorator(aveto))
    try:
        hook(
            risk_version,
            "testapp.awrapped_veto",
            timing="before",
            event="transition",
            transition="assess",
        )
        with pytest.raises(AbortTransition):
            move(risk, "risk.assess")
    finally:
        side_effect_registry.unregister("testapp.awrapped_veto")
    risk.refresh_from_db()
    assert risk.status_key == "draft"


@pytest.mark.asyncio
async def test_an_async_hook_on_a_running_loop_says_which_entry_point_to_use():
    """The one failure asgiref reports that the caller cannot act on unaided.

    It happens when synchronous ``transition()`` is reached from a thread that already
    runs a loop -- a project with ``DJANGO_ALLOW_ASYNC_UNSAFE`` set, typically. asgiref
    says only that ``AsyncToSync`` cannot be used here, naming neither the hook that
    tripped it nor the way out, so the message is replaced.  Driven through
    ``call_handler`` directly because the condition is about the calling thread, not
    about anything the engine does.
    """
    from types import SimpleNamespace

    from vinta_state_machines.side_effects import call_handler

    # Only ``context.hook.handler_key`` is read on this path, so a stand-in is honest
    # here and avoids building a graph just to reach one error message.
    context = cast("Any", SimpleNamespace(hook=SimpleNamespace(handler_key="testapp.arecord")))
    with pytest.raises(RuntimeError, match=r"testapp\.arecord.*atransition"):
        call_handler(side_effects.arecord, context)


@pytest.mark.asyncio
async def test_an_unrelated_runtime_error_from_a_handler_is_left_alone():
    """Only asgiref's refusal is translated; the handler's own errors pass through."""
    from types import SimpleNamespace

    from vinta_state_machines.side_effects import call_handler

    async def handler(context):
        raise RuntimeError("something else entirely")

    context = cast("Any", SimpleNamespace(hook=SimpleNamespace(handler_key="x")))
    with pytest.raises(RuntimeError, match="something else entirely"):
        await sync_to_async(call_handler, thread_sensitive=False)(handler, context)


# --------------------------------------------------------------- engine twins


def test_the_async_twins_answer_what_the_sync_ones_do(risk_version, risk):
    assert async_to_sync(acan_transition)(risk, "risk.assess") is True
    assert "risk.assess" in async_to_sync(aavailable_actions)(risk)
    # ``risk.discard`` requires approval, so it is blocked unless asked for.
    assert [item.action for item in async_to_sync(aavailable_transitions)(risk)] == ["risk.assess"]
    blocked = async_to_sync(aavailable_transitions)(risk, include_blocked=True)
    assert [item.action for item in blocked] == ["risk.assess", "risk.discard"]
    assert async_to_sync(acurrent_state)(risk).key == "draft"
    assert async_to_sync(aresolve_version)(risk) == risk_version
    assert async_to_sync(agraph_for)(risk).machine_key == "risk.status"
    assert async_to_sync(ainitial_status_key)(Risk) == "draft"


def test_atransition_returns_the_history_row_and_mutates_the_instance(risk_version, risk):
    record = async_to_sync(atransition)(risk, "risk.assess")
    assert record.to_status.key == "assessed"
    # Mutated in place, without a refresh, exactly as the synchronous call leaves it.
    assert risk.status_key == "assessed"


def test_the_mixin_exposes_the_async_twins(risk_version, risk):
    assert async_to_sync(risk.acan_transition)("risk.assess") is True
    async_to_sync(risk.atransition)("risk.assess")
    assert risk.status_key == "assessed"
    assert async_to_sync(risk.acurrent_state)().key == "assessed"


def test_a_refused_move_raises_through_the_async_twin(risk_version, risk):
    from vinta_state_machines.exceptions import TransitionNotAllowed

    with pytest.raises(TransitionNotAllowed):
        move(risk, "risk.mitigate")


# ------------------------------------------------------- the genuinely async path


# Fixtures and the ``hook`` helper both hit the ORM, which an async test may not do on
# the loop -- hence ``sync_to_async`` around the setup rather than around the assertion.
abind = sync_to_async(hook, thread_sensitive=True)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_atransition_works_from_a_real_event_loop(risk_version, risk):
    """The production shape: an async caller, the ORM on an executor thread."""
    record = await atransition(risk, "risk.assess")
    assert risk.status_key == "assessed"
    # ``to_status`` is a lazy foreign key, so reading it is a query and belongs off
    # the loop like every other one.
    assert await sync_to_async(lambda: record.to_status.key)() == "assessed"


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_an_async_hook_is_awaited_on_the_callers_loop(risk_version, risk):
    """The reason ``atransition`` exists rather than ``sync_to_async(transition)``.

    A handler awaited on a throwaway loop cannot share a connection pool, an httpx
    client or anything else the caller set up, so which loop it lands on is a
    correctness property, not an implementation detail.
    """
    await abind(risk_version, timing="after", event="transition", transition="assess")
    caller_loop = id(asyncio.get_running_loop())
    await atransition(risk, "risk.assess")
    assert side_effects.CALLS[0]["loop_id"] == caller_loop


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_deferred_async_hook_also_lands_on_the_callers_loop(risk_version, risk):
    """``on_commit`` runs past the commit but still inside the executor call, so the
    loop the move borrowed is still the one asgiref hands the coroutine back to."""
    await abind(
        risk_version, timing="after", event="transition", transition="assess", on_commit=True
    )
    caller_loop = id(asyncio.get_running_loop())
    await atransition(risk, "risk.assess")
    assert side_effects.CALLS[0]["handler"] == "testapp.arecord"
    assert side_effects.CALLS[0]["loop_id"] == caller_loop


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_an_async_veto_rolls_back_from_a_real_event_loop(risk_version, risk):
    await abind(
        risk_version, "testapp.aveto", timing="before", event="transition", transition="assess"
    )
    with pytest.raises(AbortTransition):
        await atransition(risk, "risk.assess")
    await risk.arefresh_from_db()
    assert risk.status_key == "draft"
