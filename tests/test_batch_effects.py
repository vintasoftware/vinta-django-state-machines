"""Wiring a batch into a graph: the two handlers, the helper, and the pairing rule."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

import pytest

from tests.testapp.models import ImportRow, ImportRun
from vinta_state_machines.batch_effects import (
    OPEN_HANDLER,
    REPORT_HANDLER,
    wire_batch_reporting,
)
from vinta_state_machines.batches import live_batch_for
from vinta_state_machines.enums import BatchLifecycle, HookEvent, HookTiming
from vinta_state_machines.graph import HookSpec
from vinta_state_machines.models import StateMachineHook, StatusBatch
from vinta_state_machines.services import validate_version
from vinta_state_machines.side_effects import registered_side_effects

pytestmark = pytest.mark.django_db


@pytest.fixture
def commits(django_capture_on_commit_callbacks):
    @contextmanager
    def _commit():
        with django_capture_on_commit_callbacks(execute=True):
            yield

    return _commit


def wire_open(version, **params):
    """Bind the shipped opener to entering ``processing``."""
    return StateMachineHook.objects.create(
        state_machine_version=version,
        handler_key=OPEN_HANDLER,
        timing=HookTiming.AFTER,
        event=HookEvent.ENTER_STATE,
        state=version.states.get(status__key="processing"),
        params={"join_action": "import_run.finish", **params},
    )


# ------------------------------------------------------------------ registration


def test_the_app_registers_its_own_handlers():
    """A version can reference them by key without the project writing anything."""
    keys = registered_side_effects()

    assert OPEN_HANDLER in keys
    assert REPORT_HANDLER in keys


# -------------------------------------------------------------------- opening


def test_entering_the_waiting_state_opens_a_batch(run_version):
    wire_open(run_version)
    run = ImportRun.objects.create(label="nightly")

    run.transition("import_run.start")

    batch = live_batch_for(run)
    assert batch is not None
    assert batch.join_action.key == "import_run.finish"


def test_a_timeout_param_is_read_as_an_iso_duration(run_version):
    """params come from JSON, so a duration arrives as a string."""
    wire_open(run_version, timeout="PT2H")
    run = ImportRun.objects.create(label="nightly")

    run.transition("import_run.start")

    batch = live_batch_for(run)
    assert batch.timeout_at is not None


def test_a_join_retry_override_rides_in_params_too(run_version):
    wire_open(run_version, join_retry_after="PT30M")
    run = ImportRun.objects.create(label="nightly")

    run.transition("import_run.start")

    assert live_batch_for(run).join_retry_after == timedelta(minutes=30)


def test_a_total_in_params_seals_the_batch_immediately(run_version):
    wire_open(run_version, total=3)
    run = ImportRun.objects.create(label="nightly")

    run.transition("import_run.start")

    batch = live_batch_for(run)
    assert (batch.sealed, batch.total) == (True, 3)


def test_opening_twice_is_harmless(run_version):
    """The transition could be retried; the constraint is what makes that safe."""
    wire_open(run_version)
    run = ImportRun.objects.create(label="nightly")
    run.transition("import_run.start")

    from vinta_state_machines.batch_effects import open_batch_effect
    from vinta_state_machines.side_effects import SideEffectContext

    graph = run.state_machine_graph()
    binding = HookSpec(
        pk=0,
        handler_key=OPEN_HANDLER,
        timing=HookTiming.AFTER,
        event=HookEvent.ENTER_STATE,
        transition_pk=None,
        state_key="processing",
        params={"join_action": "import_run.finish"},
        order=0,
        on_commit=False,
    )
    open_batch_effect(
        SideEffectContext(
            instance=run,
            field_name="status_key",
            status_field="status",
            from_status="pending",
            to_status="processing",
            action="import_run.start",
            version=run.state_machine_version(),
            graph=graph,
            transition=graph.transitions[0],
            timing=HookTiming.AFTER,
            event=HookEvent.ENTER_STATE,
            hook=binding,
            params={"join_action": "import_run.finish"},
        )
    )

    assert StatusBatch.objects.count() == 1


# ------------------------------------------------------------------ reporting


def test_arriving_at_a_finished_state_counts_the_child(waiting_run, row_version, commits):
    wire_batch_reporting(row_version, {"processed": "success", "rejected": "failure"})
    batch = StatusBatch.objects.create(**_batch_values(waiting_run, sealed=True, total=2))
    row = ImportRow.objects.create(run=waiting_run, batch=batch)

    with commits():
        row.transition("import_row.process")

    batch.refresh_from_db()
    row.refresh_from_db()
    assert batch.finished == 1
    assert batch.succeeded == 1
    assert row.batch_reported_at is not None


def test_a_rejected_child_counts_as_a_failure(waiting_run, row_version, commits):
    wire_batch_reporting(row_version, {"processed": "success", "rejected": "failure"})
    batch = StatusBatch.objects.create(**_batch_values(waiting_run, sealed=True, total=2))
    row = ImportRow.objects.create(run=waiting_run, batch=batch)

    with commits():
        row.transition("import_row.reject")

    batch.refresh_from_db()
    assert (batch.finished, batch.succeeded, batch.failed) == (1, 0, 1)


def test_leaving_a_finished_state_un_counts_the_child(waiting_run, row_version, commits):
    """Reopening a processed row has to bring the counter back with it."""
    wire_batch_reporting(row_version, {"processed": "success", "rejected": "failure"})
    batch = StatusBatch.objects.create(**_batch_values(waiting_run, sealed=True, total=2))
    row = ImportRow.objects.create(run=waiting_run, batch=batch)
    with commits():
        row.transition("import_row.process")

    with commits():
        row.transition("import_row.reopen")

    batch.refresh_from_db()
    row.refresh_from_db()
    assert (batch.finished, batch.succeeded) == (0, 0)
    assert row.batch_reported_at is None


def test_the_last_child_finishing_moves_the_parent(waiting_run, row_version, commits):
    """The whole thing, end to end, driven only by graph data."""
    wire_batch_reporting(row_version, {"processed": "success", "rejected": "failure"})
    batch = StatusBatch.objects.create(**_batch_values(waiting_run, sealed=True, total=2))
    first = ImportRow.objects.create(run=waiting_run, batch=batch)
    second = ImportRow.objects.create(run=waiting_run, batch=batch)

    with commits():
        first.transition("import_row.process")
        second.transition("import_row.process")

    waiting_run.refresh_from_db()
    batch.refresh_from_db()
    assert waiting_run.status_key == "completed"
    assert batch.lifecycle == BatchLifecycle.CLOSED


# --------------------------------------------------------------------- wiring


def test_the_helper_writes_both_halves_for_a_reopenable_state(row_version):
    wire_batch_reporting(row_version, {"processed": "success"})

    events = set(
        row_version.hooks.filter(handler_key=REPORT_HANDLER).values_list("event", flat=True)
    )

    assert events == {HookEvent.ENTER_STATE, HookEvent.LEAVE_STATE}


def test_the_helper_writes_only_the_enter_half_for_a_terminal_state(row_version):
    """A leave binding on a terminal state could never fire, so there is none to write."""
    wire_batch_reporting(row_version, {"rejected": "failure"})

    events = list(
        row_version.hooks.filter(handler_key=REPORT_HANDLER).values_list("event", flat=True)
    )

    assert events == [HookEvent.ENTER_STATE]


def test_the_helper_is_idempotent(row_version):
    wire_batch_reporting(row_version, {"processed": "success"})
    wire_batch_reporting(row_version, {"processed": "success"})

    assert row_version.hooks.filter(handler_key=REPORT_HANDLER).count() == 2


def test_the_outcome_travels_on_both_rows(row_version):
    """Un-counting has to know what it is undoing."""
    wire_batch_reporting(row_version, {"processed": "success"})

    for hook in row_version.hooks.filter(handler_key=REPORT_HANDLER):
        assert hook.params["outcome"] == "success"


def test_an_explicit_batch_field_is_carried_through(row_version):
    wire_batch_reporting(row_version, {"processed": "success"}, batch_field="batch")

    hook = row_version.hooks.filter(handler_key=REPORT_HANDLER).first()
    assert hook.params["batch_field"] == "batch"


# ---------------------------------------------------------------- the pairing


def test_a_wired_version_validates(row_draft):
    wire_batch_reporting(row_draft, {"processed": "success", "rejected": "failure"})

    assert validate_version(row_draft).ok


def test_counting_without_un_counting_on_a_reopenable_state_is_an_error(row_draft):
    """A child that can come back must be able to bring the counter back with it."""
    StateMachineHook.objects.create(
        state_machine_version=row_draft,
        handler_key=REPORT_HANDLER,
        timing=HookTiming.AFTER,
        event=HookEvent.ENTER_STATE,
        state=row_draft.states.get(status__key="processed"),
        params={"outcome": "success"},
    )

    report = validate_version(row_draft)

    assert not report.ok
    assert any("never un-counts" in error for error in report.errors)


def test_counting_without_un_counting_on_a_terminal_state_is_fine(row_draft):
    """The exemption that lets the rule above be an error rather than a warning."""
    StateMachineHook.objects.create(
        state_machine_version=row_draft,
        handler_key=REPORT_HANDLER,
        timing=HookTiming.AFTER,
        event=HookEvent.ENTER_STATE,
        state=row_draft.states.get(status__key="rejected"),
        params={"outcome": "failure"},
    )

    assert validate_version(row_draft).ok


def test_un_counting_a_terminal_state_is_dead_wiring_and_warns(row_draft):
    StateMachineHook.objects.create(
        state_machine_version=row_draft,
        handler_key=REPORT_HANDLER,
        timing=HookTiming.AFTER,
        event=HookEvent.LEAVE_STATE,
        state=row_draft.states.get(status__key="rejected"),
        params={"outcome": "failure"},
    )

    report = validate_version(row_draft)

    assert report.ok
    assert any("can never fire" in warning for warning in report.warnings)


def test_a_state_with_no_report_hooks_is_not_the_check_s_business(row_draft):
    assert validate_version(row_draft).ok


def _batch_values(run, **overrides):
    from django.contrib.contenttypes.models import ContentType

    from vinta_state_machines.identities import resolve_identity
    from vinta_state_machines.models import ActionType, StatusDefinition

    values = {
        "target_type": ContentType.objects.get_for_model(run),
        "target_id": str(run.pk),
        "status_field": "status_key",
        "opened_in_status": StatusDefinition.objects.get(
            entity_type="import_run", status_field="status", key="processing"
        ),
        "state_machine_version": run.state_machine_version(),
        "join_action": ActionType.objects.get(key="import_run.finish"),
        "actor": resolve_identity(None),
    }
    values.update(overrides)
    return values
