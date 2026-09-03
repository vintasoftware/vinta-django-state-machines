"""Cancelling: the batch always stops, the children only if you say so."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from tests.testapp.models import ImportNote, ImportRow
from vinta_state_machines.batch_effects import ABANDON_HANDLER, wire_batch_reporting
from vinta_state_machines.batches import (
    CANCEL,
    SUCCESS,
    abandon,
    count_child,
    open_batch,
    run_batch_operation,
)
from vinta_state_machines.enums import BatchFailureReason, BatchLifecycle, HookEvent, HookTiming
from vinta_state_machines.models import StateMachineHook, StatusBatch, StatusBatchReport

pytestmark = pytest.mark.django_db


@pytest.fixture
def commits(django_capture_on_commit_callbacks):
    @contextmanager
    def _commit():
        with django_capture_on_commit_callbacks(execute=True):
            yield

    return _commit


def a_batch(run, **kwargs):
    kwargs.setdefault("join_action", "import_run.finish")
    return open_batch(run, **kwargs)


def wire_abandon(version, **params):
    """Bind the shipped handler to the cancel edge of the parent's machine."""
    edge = version.transitions.get(name="cancel")
    return StateMachineHook.objects.create(
        state_machine_version=version,
        handler_key=ABANDON_HANDLER,
        timing=HookTiming.AFTER,
        event=HookEvent.TRANSITION,
        transition=edge,
        params=params,
    )


# ------------------------------------------------------------------ abandoning


def test_abandoning_stops_the_batch(waiting_run):
    batch = a_batch(waiting_run, total=5)

    assert abandon(batch, reason=BatchFailureReason.CANCELLED) is True

    batch.refresh_from_db()
    assert batch.lifecycle == BatchLifecycle.ABANDONED
    assert batch.failure_reason == "cancelled"


def test_abandoning_twice_is_a_no_op(waiting_run):
    batch = a_batch(waiting_run, total=5)
    abandon(batch, reason=BatchFailureReason.CANCELLED)

    assert abandon(batch, reason=BatchFailureReason.CANCELLED) is False


def test_a_closed_batch_is_never_dragged_back_to_abandoned(waiting_run):
    batch = a_batch(waiting_run, total=1)
    StatusBatch.objects.filter(pk=batch.pk).update(lifecycle=BatchLifecycle.CLOSED)

    assert abandon(batch, reason=BatchFailureReason.CANCELLED) is False


def test_abandoning_clears_the_opaque_reports(waiting_run):
    batch = a_batch(waiting_run, total=2)
    StatusBatchReport.objects.create(batch=batch, key="a", outcome="success")

    abandon(batch, reason=BatchFailureReason.CANCELLED)

    assert not StatusBatchReport.objects.filter(batch=batch).exists()


def test_an_abandoned_batch_frees_the_record_for_a_new_one(waiting_run):
    batch = a_batch(waiting_run, total=1)
    abandon(batch, reason=BatchFailureReason.CANCELLED)

    again = a_batch(waiting_run, total=1)

    assert again.pk != batch.pk


# ---------------------------------------------------- the batch always stops


def test_cancelling_the_parent_abandons_the_batch(waiting_run, run_version):
    """Not optional. A batch left open would fire a join at a cancelled record."""
    wire_abandon(run_version)
    batch = a_batch(waiting_run, total=5)

    waiting_run.transition("import_run.cancel")

    batch.refresh_from_db()
    assert batch.lifecycle == BatchLifecycle.ABANDONED
    assert batch.failure_reason == "cancelled"


def test_a_child_finishing_after_a_cancel_cannot_move_the_parent(
    waiting_run, run_version, row_version, commits
):
    """The whole reason abandoning is not a choice."""
    wire_abandon(run_version)
    wire_batch_reporting(row_version, {"processed": "success", "rejected": "failure"})
    batch = a_batch(waiting_run, total=1)
    row = ImportRow.objects.create(run=waiting_run, batch=batch)
    waiting_run.transition("import_run.cancel")

    with commits():
        row.transition("import_row.process")

    waiting_run.refresh_from_db()
    batch.refresh_from_db()
    assert waiting_run.status_key == "cancelled"
    assert batch.lifecycle == BatchLifecycle.ABANDONED


# --------------------------------------------------- the children are a choice


def test_children_keep_running_by_default(waiting_run, run_version, row_version, commits):
    wire_abandon(run_version, child_cancel_action="import_row.reject")
    batch = a_batch(waiting_run, total=2)
    row = ImportRow.objects.create(run=waiting_run, batch=batch)

    with commits():
        waiting_run.transition("import_run.cancel")

    row.refresh_from_db()
    assert row.status_key == "queued"


def test_the_caller_can_ask_for_the_children_to_stop(
    waiting_run, run_version, row_version, commits
):
    """The per-cancellation choice, passed in metadata rather than baked in the graph."""
    wire_abandon(run_version, child_cancel_action="import_row.reject")
    batch = a_batch(waiting_run, total=2)
    row = ImportRow.objects.create(run=waiting_run, batch=batch)

    with commits():
        waiting_run.transition("import_run.cancel", metadata={"cancel_children": True})

    row.refresh_from_db()
    assert row.status_key == "rejected"


def test_the_graph_can_make_stopping_the_children_the_default(
    waiting_run, run_version, row_version, commits
):
    wire_abandon(run_version, cancel_children=True, child_cancel_action="import_row.reject")
    batch = a_batch(waiting_run, total=2)
    row = ImportRow.objects.create(run=waiting_run, batch=batch)

    with commits():
        waiting_run.transition("import_run.cancel")

    row.refresh_from_db()
    assert row.status_key == "rejected"


def test_the_call_site_can_override_the_graph_s_default(
    waiting_run, run_version, row_version, commits
):
    wire_abandon(run_version, cancel_children=True, child_cancel_action="import_row.reject")
    batch = a_batch(waiting_run, total=2)
    row = ImportRow.objects.create(run=waiting_run, batch=batch)

    with commits():
        waiting_run.transition("import_run.cancel", metadata={"cancel_children": False})

    row.refresh_from_db()
    assert row.status_key == "queued"


# ------------------------------------------------------------------- cascade


def test_a_child_already_finished_is_left_alone(waiting_run, run_version, row_version, commits):
    """It is done. Cancelling it would rewrite history that already happened."""
    wire_abandon(run_version, child_cancel_action="import_row.reject")
    wire_batch_reporting(row_version, {"processed": "success", "rejected": "failure"})
    batch = a_batch(waiting_run, total=2)
    done = ImportRow.objects.create(run=waiting_run, batch=batch)
    with commits():
        done.transition("import_row.process")

    with commits():
        waiting_run.transition("import_run.cancel", metadata={"cancel_children": True})

    done.refresh_from_db()
    assert done.status_key == "processed"


def test_a_child_with_no_cancel_edge_is_left_to_run(
    waiting_run, run_version, row_version, commits
):
    """Nothing raises, and no child machine has to grow an edge it did not want."""
    wire_abandon(run_version, child_cancel_action="import_row.no_such_action")
    batch = a_batch(waiting_run, total=2)
    row = ImportRow.objects.create(run=waiting_run, batch=batch)

    with commits():
        waiting_run.transition("import_run.cancel", metadata={"cancel_children": True})

    row.refresh_from_db()
    batch.refresh_from_db()
    assert row.status_key == "queued"
    assert batch.lifecycle == BatchLifecycle.ABANDONED


def test_the_skip_is_logged_once_per_model_and_action(
    waiting_run, run_version, row_version, commits, caplog
):
    """Enough to catch a typo in child_cancel_action; not one line per row."""
    wire_abandon(run_version, child_cancel_action="import_row.no_such_action")
    batch = a_batch(waiting_run, total=3)
    for _ in range(3):
        ImportRow.objects.create(run=waiting_run, batch=batch)

    with caplog.at_level("INFO", logger="vinta_state_machines.batches"), commits():
        waiting_run.transition("import_run.cancel", metadata={"cancel_children": True})

    messages = [r for r in caplog.records if "no usable" in r.getMessage()]
    assert len(messages) == 1


def test_a_child_with_no_governed_status_is_skipped(waiting_run, run_version, commits):
    """There is no status to move it on, so there is nothing to cancel."""
    wire_abandon(run_version, child_cancel_action="import_row.reject")
    batch = a_batch(waiting_run, total=1)
    ImportNote.objects.create(batch=batch)

    with commits():
        waiting_run.transition("import_run.cancel", metadata={"cancel_children": True})

    batch.refresh_from_db()
    assert batch.lifecycle == BatchLifecycle.ABANDONED


def test_the_cascade_is_dispatched_not_run_inline(waiting_run, run_version, row_version):
    """A million transitions do not belong in the cancelling user's request."""
    wire_abandon(run_version, child_cancel_action="import_row.reject")
    batch = a_batch(waiting_run, total=1)
    row = ImportRow.objects.create(run=waiting_run, batch=batch)

    waiting_run.transition("import_run.cancel", metadata={"cancel_children": True})

    row.refresh_from_db()
    assert row.status_key == "queued"  # deferred; nothing has run yet
    batch.refresh_from_db()
    assert batch.metadata["child_cancel_action"] == "import_row.reject"


def test_running_the_cascade_twice_is_harmless(waiting_run, run_version, row_version, commits):
    wire_abandon(run_version, child_cancel_action="import_row.reject")
    batch = a_batch(waiting_run, total=1)
    row = ImportRow.objects.create(run=waiting_run, batch=batch)

    with commits():
        waiting_run.transition("import_run.cancel", metadata={"cancel_children": True})
    run_batch_operation(CANCEL, batch.pk)

    row.refresh_from_db()
    assert row.status_key == "rejected"


def test_a_cascade_with_no_action_recorded_does_nothing(waiting_run, row_version):
    batch = a_batch(waiting_run, total=1)
    ImportRow.objects.create(run=waiting_run, batch=batch)

    run_batch_operation(CANCEL, batch.pk)  # no child_cancel_action in metadata

    assert ImportRow.objects.get().status_key == "queued"


def test_a_cascade_on_a_vanished_batch_does_nothing():
    run_batch_operation(CANCEL, 999999)


def test_nested_batches_cascade_down_one_level_at_a_time(
    waiting_run, run_version, row_version, commits
):
    """A child's own cancel fires its own abandon hook, which is the whole recursion."""
    wire_abandon(run_version, child_cancel_action="import_row.reject")
    parent = a_batch(waiting_run, total=1)
    row = ImportRow.objects.create(run=waiting_run, batch=parent)
    nested = open_batch(
        row,
        join_action="import_row.process",
        parent_batch=parent,
    )

    with commits():
        waiting_run.transition("import_run.cancel", metadata={"cancel_children": True})

    assert nested.depth == parent.depth + 1
    row.refresh_from_db()
    assert row.status_key == "rejected"


def test_counting_into_an_abandoned_batch_still_stamps_but_does_not_count(
    waiting_run, row_version
):
    """The stamp is idempotency; the counter is what an abandoned batch stops moving."""
    batch = a_batch(waiting_run, total=2)
    row = ImportRow.objects.create(run=waiting_run, batch=batch)
    abandon(batch, reason=BatchFailureReason.CANCELLED)

    count_child(row, outcome=SUCCESS)

    batch.refresh_from_db()
    row.refresh_from_db()
    assert batch.finished == 0
    assert row.batch_reported_at is not None
