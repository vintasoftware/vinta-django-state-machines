"""``SideEffectRun``: what each handler did, and the transaction rules that shape it."""

from __future__ import annotations

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import override_settings

from tests.testapp.models import Risk
from vinta_state_machines.engine import transition
from vinta_state_machines.enums import SideEffectOutcome
from vinta_state_machines.models import SideEffectRun, StateMachineHook
from vinta_state_machines.side_effects import AbortTransition

pytestmark = pytest.mark.django_db


def hook(version, handler="testapp.record", **kwargs):
    state_key = kwargs.pop("state", None)
    edge = kwargs.pop("transition", None)
    if state_key:
        kwargs["state"] = version.states.get(status__key=state_key)
    if edge:
        kwargs["transition"] = version.transitions.get(name=edge)
    return StateMachineHook.objects.create(
        state_machine_version=version, handler_key=handler, **kwargs
    )


def recording(mode: str, **extra):
    """Turn run recording to ``mode`` for one test."""
    return override_settings(
        STATE_MACHINES={
            "CACHE_GRAPHS": False,
            "RECORD_SIDE_EFFECT_RUNS": mode,
            **extra,
        }
    )


# ------------------------------------------------------------------ how much is kept


def test_the_default_keeps_only_the_runs_that_went_wrong(risk_version, risk):
    hook(risk_version, timing="after", transition="assess")

    risk.transition("risk.assess")

    assert not SideEffectRun.objects.exists()


def test_all_mode_writes_a_row_per_handler(risk_version, risk):
    hook(risk_version, timing="after", transition="assess")
    hook(risk_version, timing="before", transition="assess")

    with recording("all"):
        risk.transition("risk.assess")

    assert SideEffectRun.objects.count() == 2
    assert set(SideEffectRun.objects.values_list("timing", flat=True)) == {"before", "after"}


def test_none_mode_writes_nothing_even_for_a_failure(risk_version, risk):
    hook(risk_version, handler="testapp.boom", timing="after", transition="assess")

    with recording("none"), pytest.raises(RuntimeError):
        risk.transition("risk.assess")

    assert not SideEffectRun.objects.exists()


def test_one_hook_may_override_the_setting(risk_version, risk):
    hook(risk_version, timing="after", transition="assess", record_runs="all")
    hook(risk_version, handler="testapp.bump_amount", timing="after", transition="assess")

    risk.transition("risk.assess")

    assert [run.handler_key for run in SideEffectRun.objects.all()] == ["testapp.record"]


# ----------------------------------------------------------------- what a row holds


def test_a_successful_run_carries_both_stamps_and_a_duration(risk_version, risk):
    hook(risk_version, timing="after", transition="assess")

    with recording("all"):
        risk.transition("risk.assess")

    run = SideEffectRun.objects.get()
    assert run.outcome == SideEffectOutcome.SUCCEEDED
    assert run.started_at is not None
    assert run.completed_at is not None
    assert run.completed_at >= run.started_at
    assert run.duration_ms is not None
    assert run.error_class == ""


def test_a_run_describes_the_move_it_belonged_to(risk_version, risk):
    hook(risk_version, timing="after", transition="assess")

    with recording("all"):
        record = risk.transition("risk.assess")

    run = SideEffectRun.objects.get()
    assert run.handler_key == "testapp.record"
    assert run.from_status_key == "draft"
    assert run.to_status_key == "assessed"
    assert run.action_key == "risk.assess"
    assert run.status_field == "status"
    assert run.state_machine_version_id == risk_version.pk
    assert run.status_transition_id == record.pk


def test_a_run_points_at_the_record_that_moved(risk_version, risk):
    hook(risk_version, timing="after", transition="assess")

    with recording("all"):
        risk.transition("risk.assess")

    run = SideEffectRun.objects.get()
    assert run.target_id == str(risk.pk)
    assert run.target_type == ContentType.objects.get_for_model(Risk)
    assert run.target == risk


def test_a_run_carries_the_scope_of_the_machine_that_authorised_it(risk_version, risk):
    hook(risk_version, timing="after", transition="assess")

    with recording("all"):
        risk.transition("risk.assess")

    run = SideEffectRun.objects.get()
    assert run.scope_id == risk_version.state_machine.scope_id
    assert run.scope_key == ""


def test_a_before_hook_is_linked_to_the_row_its_move_eventually_wrote(risk_version, risk):
    """It ran before the row existed, but by flush time it is the same move."""
    hook(risk_version, timing="before", transition="assess")

    with recording("all"):
        record = risk.transition("risk.assess")

    assert SideEffectRun.objects.get().status_transition_id == record.pk


def test_a_before_hook_on_an_unsaved_record_still_learns_its_id(risk_version):
    """The pk is read at flush time, which is half the point of buffering."""
    hook(risk_version, timing="before", transition="create")
    fresh = Risk(title="Fresh", amount=1)

    with recording("all"):
        transition(fresh, "risk.create")

    assert fresh.pk is not None
    assert SideEffectRun.objects.get().target_id == str(fresh.pk)


# --------------------------------------------------------------------- failures


def test_a_failing_handler_is_recorded_even_though_the_move_rolled_back(risk_version, risk):
    """The case the obvious implementation loses."""
    hook(risk_version, handler="testapp.boom", timing="after", transition="assess")

    with pytest.raises(RuntimeError):
        risk.transition("risk.assess")

    risk.refresh_from_db()
    assert risk.status_key == "draft"
    run = SideEffectRun.objects.get()
    assert run.outcome == SideEffectOutcome.FAILED
    assert run.error_class == "builtins.RuntimeError"
    assert run.status_transition_id is None


def test_a_veto_is_recorded_as_an_abort_rather_than_a_failure(risk_version, risk):
    hook(risk_version, handler="testapp.veto", timing="before", transition="assess")

    with pytest.raises(AbortTransition):
        risk.transition("risk.assess")

    run = SideEffectRun.objects.get()
    assert run.outcome == SideEffectOutcome.ABORTED
    assert run.error_class.endswith("AbortTransition")


def test_the_handlers_that_ran_before_the_failure_are_recorded_too(risk_version, risk):
    hook(risk_version, timing="before", transition="assess", order=0)
    hook(risk_version, handler="testapp.boom", timing="before", transition="assess", order=1)

    with recording("all"), pytest.raises(RuntimeError):
        risk.transition("risk.assess")

    outcomes = dict(SideEffectRun.objects.values_list("handler_key", "outcome"))
    assert outcomes == {
        "testapp.record": SideEffectOutcome.SUCCEEDED,
        "testapp.boom": SideEffectOutcome.FAILED,
    }


def test_the_error_message_is_withheld_by_default(risk_version, risk):
    hook(risk_version, handler="testapp.veto", timing="before", transition="assess")

    with pytest.raises(AbortTransition):
        risk.transition("risk.assess", metadata={})

    assert SideEffectRun.objects.get().error_detail == ""


def test_the_error_message_is_kept_when_the_project_asks_for_it(risk_version, risk):
    hook(risk_version, handler="testapp.veto", timing="before", transition="assess")

    with (
        recording("failures", CAPTURE_SIDE_EFFECT_ERROR_DETAIL=True),
        pytest.raises(AbortTransition),
    ):
        risk.transition("risk.assess")

    assert "vetoed" in SideEffectRun.objects.get().error_detail


def test_a_captured_error_message_is_truncated(risk_version, risk):
    hook(
        risk_version,
        handler="testapp.veto",
        timing="before",
        transition="assess",
        params={"reason": "x" * 200},
    )

    with (
        recording(
            "failures",
            CAPTURE_SIDE_EFFECT_ERROR_DETAIL=True,
            MAX_SIDE_EFFECT_ERROR_DETAIL=20,
        ),
        pytest.raises(AbortTransition),
    ):
        risk.transition("risk.assess")

    assert len(SideEffectRun.objects.get().error_detail) == 20


# ------------------------------------------------------------------ deferred hooks


def test_a_deferred_handler_records_itself_after_the_commit(
    risk_version, risk, django_capture_on_commit_callbacks
):
    hook(risk_version, timing="after", transition="assess", on_commit=True)

    with recording("all"), django_capture_on_commit_callbacks(execute=True):
        risk.transition("risk.assess")

    run = SideEffectRun.objects.get()
    assert run.outcome == SideEffectOutcome.SUCCEEDED
    assert run.completed_at is not None
    assert run.duration_ms is not None


def test_a_deferred_handler_is_linked_to_the_history_row(
    risk_version, risk, django_capture_on_commit_callbacks
):
    hook(risk_version, timing="after", transition="assess", on_commit=True)

    with recording("all"), django_capture_on_commit_callbacks(execute=True):
        record = risk.transition("risk.assess")

    assert SideEffectRun.objects.get().status_transition_id == record.pk


def test_a_failing_deferred_handler_is_recorded(
    risk_version, risk, django_capture_on_commit_callbacks
):
    hook(risk_version, handler="testapp.boom", timing="after", transition="assess", on_commit=True)

    with (
        pytest.raises(RuntimeError),
        django_capture_on_commit_callbacks(execute=True),
    ):
        risk.transition("risk.assess")

    run = SideEffectRun.objects.get()
    assert run.outcome == SideEffectOutcome.FAILED
    # The move landed before the handler ran; that is what ``on_commit`` means.
    risk.refresh_from_db()
    assert risk.status_key == "assessed"


def test_a_deferred_handler_writes_one_row_not_two(
    risk_version, risk, django_capture_on_commit_callbacks
):
    """It is written twice -- opened, then closed -- but it is one row."""
    hook(risk_version, timing="after", transition="assess", on_commit=True)

    with recording("all"), django_capture_on_commit_callbacks(execute=True):
        risk.transition("risk.assess")

    assert SideEffectRun.objects.count() == 1


# -------------------------------------------------------------------- the sink


def test_a_configured_sink_is_handed_the_rows_instead_of_the_database(risk_version, risk):
    from tests.testapp import side_effects as traces

    hook(risk_version, timing="after", transition="assess")
    traces.SINKED.clear()

    with (
        recording("all", SIDE_EFFECT_RUN_SINK="tests.testapp.side_effects.collect_runs"),
    ):
        risk.transition("risk.assess")

    assert [run.handler_key for run in traces.SINKED] == ["testapp.record"]
    assert not SideEffectRun.objects.exists()


# ------------------------------------------------------------------- housekeeping


def test_a_run_reads_as_its_own_summary(risk_version, risk):
    hook(risk_version, timing="after", transition="assess")

    with recording("all"):
        risk.transition("risk.assess")

    assert str(SideEffectRun.objects.get()) == "testapp.record -> succeeded"


def test_deleting_the_hook_leaves_the_run_and_its_key_behind(risk_version, risk):
    binding = hook(risk_version, timing="after", transition="assess")

    with recording("all"):
        risk.transition("risk.assess")
    StateMachineHook.objects.filter(pk=binding.pk).delete()

    run = SideEffectRun.objects.get()
    assert run.hook_id is None
    assert run.handler_key == "testapp.record"


def test_recording_does_not_disturb_a_transition_that_touches_the_record(risk_version, risk):
    hook(risk_version, handler="testapp.bump_amount", timing="after", transition="assess")

    with recording("all"):
        risk.transition("risk.assess")

    risk.refresh_from_db()
    assert risk.amount == 501
    assert SideEffectRun.objects.count() == 1


def test_a_transition_with_no_hooks_writes_nothing_and_costs_no_query(
    risk_version, risk, django_assert_num_queries
):
    with recording("all"):
        risk.transition("risk.assess")

    assert not SideEffectRun.objects.exists()
