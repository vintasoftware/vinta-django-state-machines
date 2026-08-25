"""Registered functions firing around a status change."""

from __future__ import annotations

import pytest
from django.db import transaction as db_transaction

from tests.testapp import side_effects
from tests.testapp.models import Risk
from vinta_state_machines.engine import transition
from vinta_state_machines.models import StateMachineHook, StatusTransition
from vinta_state_machines.registry import AlreadyRegistered, NotRegistered
from vinta_state_machines.side_effects import (
    AbortTransition,
    get_side_effect,
    register_side_effect,
    registered_side_effects,
    side_effect_registry,
)

pytestmark = pytest.mark.django_db


def hook(version, handler="testapp.record", **kwargs):
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


# ------------------------------------------------------------------- registry


def test_a_handler_is_reachable_by_its_key():
    assert get_side_effect("testapp.record") is side_effects.record
    assert "testapp.record" in registered_side_effects()


def test_registering_two_different_functions_under_one_key_is_refused():
    with pytest.raises(AlreadyRegistered, match="testapp.record"):
        register_side_effect("testapp.record")(lambda context: None)


def test_re_registering_the_same_function_is_a_no_op():
    register_side_effect("testapp.record")(side_effects.record)


def test_replacing_a_handler_is_possible_when_asked_for_explicitly():
    original = side_effect_registry.get("testapp.record")
    try:
        register_side_effect("testapp.record", replace=True)(lambda context: None)
        assert side_effect_registry.get("testapp.record") is not original
    finally:
        side_effect_registry.register("testapp.record", original, replace=True)


def test_an_unknown_key_says_which_keys_do_exist():
    with pytest.raises(NotRegistered, match="Known keys:.*testapp.record"):
        get_side_effect("nope")


# --------------------------------------------------------------------- firing


def test_a_hook_on_a_specific_transition_fires_for_that_transition_only(risk_version, risk):
    hook(risk_version, timing="after", event="transition", transition="assess")
    transition(risk, "risk.assess")
    assert traced("action") == ["risk.assess"]

    side_effects.reset()
    transition(risk, "risk.mitigate")
    assert side_effects.CALLS == []


def test_a_hook_on_any_transition_fires_for_every_edge(risk_version, risk):
    hook(risk_version, timing="after", event="any_transition")
    transition(risk, "risk.assess")
    transition(risk, "risk.mitigate")
    assert traced("action") == ["risk.assess", "risk.mitigate"]


def test_entering_and_leaving_hooks_fire_for_their_own_state(risk_version, risk):
    hook(
        risk_version, timing="after", event="enter_state", state="assessed", params={"label": "in"}
    )
    hook(
        risk_version,
        timing="after",
        event="leave_state",
        state="assessed",
        params={"label": "out"},
    )
    transition(risk, "risk.assess")
    assert traced("label") == ["in"]

    side_effects.reset()
    transition(risk, "risk.mitigate")
    assert traced("label") == ["out"]


def test_the_full_ordering_brackets_the_change_symmetrically(risk_version, risk):
    for timing in ("before", "after"):
        hook(
            risk_version,
            timing=timing,
            event="leave_state",
            state="draft",
            params={"label": f"{timing}-leave"},
        )
        hook(
            risk_version,
            timing=timing,
            event="transition",
            transition="assess",
            params={"label": f"{timing}-edge"},
        )
        hook(
            risk_version,
            timing=timing,
            event="enter_state",
            state="assessed",
            params={"label": f"{timing}-enter"},
        )
    transition(risk, "risk.assess")
    assert traced("label") == [
        "before-leave",
        "before-edge",
        "before-enter",
        "after-leave",
        "after-edge",
        "after-enter",
    ]


def test_order_breaks_ties_between_hooks_on_the_same_binding(risk_version, risk):
    hook(risk_version, timing="after", event="any_transition", order=2, params={"label": "second"})
    hook(risk_version, timing="after", event="any_transition", order=1, params={"label": "first"})
    transition(risk, "risk.assess")
    assert traced("label") == ["first", "second"]


def test_an_inactive_hook_does_not_fire(risk_version, risk):
    hook(risk_version, timing="after", event="any_transition", is_active=False)
    transition(risk, "risk.assess")
    assert side_effects.CALLS == []


# -------------------------------------------------------------------- context


def test_a_before_handler_sees_the_old_status_and_no_history_row(risk_version, risk):
    hook(risk_version, timing="before", event="any_transition")
    transition(risk, "risk.assess")
    call = side_effects.CALLS[0]
    assert (call["from"], call["to"]) == ("draft", "assessed")
    assert call["record"] is None
    assert call["instance"] is risk


def test_an_after_handler_receives_the_history_row(risk_version, risk):
    hook(risk_version, timing="after", event="any_transition")
    transition(risk, "risk.assess")
    record = side_effects.CALLS[0]["record"]
    assert isinstance(record, StatusTransition)
    assert record.to_status.key == "assessed"


def test_the_relationship_carries_a_json_parameter_for_the_handler(risk_version, risk):
    hook(
        risk_version,
        timing="after",
        event="any_transition",
        params={"template": "assessed", "cc": ["risk@example.com"], "retries": 2},
    )
    transition(risk, "risk.assess")
    assert side_effects.CALLS[0]["params"] == {
        "template": "assessed",
        "cc": ["risk@example.com"],
        "retries": 2,
    }


def test_the_same_handler_gets_different_params_on_different_bindings(risk_version, risk):
    """One function, wired twice, parameterised per binding."""
    hook(
        risk_version,
        timing="after",
        event="transition",
        transition="assess",
        params={"label": "edge"},
    )
    hook(
        risk_version,
        timing="after",
        event="enter_state",
        state="assessed",
        params={"label": "state"},
    )
    transition(risk, "risk.assess")
    assert traced("label") == ["edge", "state"]


def test_params_default_to_an_empty_mapping(risk_version, risk):
    hook(risk_version, timing="after", event="any_transition")
    transition(risk, "risk.assess")
    assert side_effects.CALLS[0]["params"] == {}


def test_params_are_separate_from_the_callers_metadata(risk_version, risk):
    hook(risk_version, timing="after", event="any_transition", params={"who": "binding"})
    transition(risk, "risk.assess", metadata={"who": "caller"})
    call = side_effects.CALLS[0]
    assert (call["params"], call["metadata"]) == ({"who": "binding"}, {"who": "caller"})


def test_params_travel_with_the_version_when_it_is_cloned(risk_version, risk):
    from vinta_state_machines.services import clone_version, publish_version

    hook(risk_version, timing="after", event="any_transition", params={"label": "carried"})
    clone = clone_version(risk_version, "2")
    publish_version(clone)

    transition(Risk.objects.create(title="On v2"), "risk.assess")
    assert traced("label") == ["carried"]


def test_the_caller_metadata_reaches_the_handler(risk_version, risk):
    hook(risk_version, timing="after", event="any_transition")
    transition(risk, "risk.assess", metadata={"ticket": "SEC-1"})
    assert side_effects.CALLS[0]["metadata"] == {"ticket": "SEC-1"}


def test_a_before_handler_persists_the_fields_it_touches(risk_version, risk):
    hook(
        risk_version,
        "testapp.bump_amount",
        timing="before",
        event="any_transition",
        params={"by": 7},
    )
    transition(risk, "risk.assess")
    risk.refresh_from_db()
    assert (risk.amount, risk.status_key) == (507, "assessed")


def test_an_after_handler_persists_the_fields_it_touches(risk_version, risk):
    hook(
        risk_version,
        "testapp.bump_amount",
        timing="after",
        event="any_transition",
        params={"by": 7},
    )
    transition(risk, "risk.assess")
    risk.refresh_from_db()
    assert risk.amount == 507


def test_a_mutation_the_handler_never_touches_is_not_persisted(risk_version, risk):
    """The engine writes the status columns and nothing else, on purpose."""
    hook(
        risk_version,
        "testapp.bump_amount_without_touching",
        timing="before",
        event="any_transition",
    )
    transition(risk, "risk.assess")
    risk.refresh_from_db()
    assert (risk.amount, risk.status_key) == (500, "assessed")


# --------------------------------------------------------------------- aborts


def test_a_before_handler_can_veto_the_whole_transition(risk_version, risk):
    hook(
        risk_version,
        "testapp.veto",
        timing="before",
        event="any_transition",
        params={"reason": "not yet"},
    )
    with pytest.raises(AbortTransition, match="not yet"):
        transition(risk, "risk.assess")
    risk.refresh_from_db()
    assert risk.status_key == "draft"
    assert StatusTransition.objects.for_object(risk).count() == 0


def test_a_veto_stops_the_handlers_that_would_have_run_after_it(risk_version, risk):
    hook(risk_version, "testapp.veto", timing="before", event="leave_state", state="draft")
    hook(risk_version, timing="before", event="enter_state", state="assessed")
    with pytest.raises(AbortTransition):
        transition(risk, "risk.assess")
    assert side_effects.CALLS == []


def test_a_failing_after_handler_rolls_the_whole_move_back(risk_version, risk):
    hook(risk_version, "testapp.boom", timing="after", event="any_transition")
    with pytest.raises(RuntimeError, match="boom"):
        transition(risk, "risk.assess")
    risk.refresh_from_db()
    assert risk.status_key == "draft"
    assert StatusTransition.objects.for_object(risk).count() == 0


def test_a_hook_referencing_an_unregistered_handler_fails_loudly(risk_version, risk):
    hook(risk_version, "nobody.registered.this", timing="after", event="any_transition")
    with pytest.raises(NotRegistered, match="nobody.registered.this"):
        transition(risk, "risk.assess")


# ----------------------------------------------------------------- on_commit


def test_an_on_commit_hook_waits_for_the_surrounding_transaction(risk_version, risk):
    hook(risk_version, timing="after", event="any_transition", on_commit=True)
    with db_transaction.atomic():
        transition(risk, "risk.assess")
        assert side_effects.CALLS == []
    # pytest-django wraps each test in a transaction that never commits, so the hook is
    # observed through captureOnCommitCallbacks instead.
    assert side_effects.CALLS == []


def test_on_commit_hooks_run_when_the_transaction_actually_commits(
    risk_version, risk, django_capture_on_commit_callbacks
):
    hook(risk_version, timing="after", event="any_transition", on_commit=True)
    with django_capture_on_commit_callbacks(execute=True):
        transition(risk, "risk.assess")
    assert traced("action") == ["risk.assess"]


def test_on_commit_is_ignored_for_before_hooks(risk_version, risk):
    hook(risk_version, timing="before", event="any_transition", on_commit=True)
    with db_transaction.atomic():
        transition(risk, "risk.assess")
        assert traced("timing") == ["before"]


# ----------------------------------------------------- hooks travel with versions


def test_hooks_are_scoped_to_their_version(risk_machine, risk_version, risk):
    from vinta_state_machines.services import clone_version, publish_version

    hook(risk_version, timing="after", event="any_transition")
    clone = clone_version(risk_version, "2")
    clone.hooks.all().delete()
    publish_version(clone)

    fresh = Risk.objects.create(title="On version 2")
    transition(fresh, "risk.assess")
    assert side_effects.CALLS == []

    transition(risk, "risk.assess")  # still on version 1, still hooked
    assert traced("action") == ["risk.assess"]
