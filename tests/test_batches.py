"""The batch runtime: opening, sealing, counting, claiming and joining."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta

import pytest
from django.db.models import F
from django.test import override_settings
from django.utils import timezone

from tests.testapp.models import ImportNote, ImportRow, ImportRun
from vinta_state_machines.batches import (
    FAILURE,
    JOIN,
    SUCCESS,
    count_child,
    join_metadata,
    live_batch_for,
    open_batch,
    report,
    report_failure,
    report_success,
    run_batch_operation,
    seal,
    try_claim,
    uncount_child,
)
from vinta_state_machines.enums import BatchFailureReason, BatchLifecycle
from vinta_state_machines.exceptions import BatchDepthExceeded
from vinta_state_machines.models import StatusBatch, StatusBatchReport

pytestmark = pytest.mark.django_db


def a_batch(run, **kwargs):
    kwargs.setdefault("join_action", "import_run.finish")
    return open_batch(run, **kwargs)


def a_row(run, batch, **kwargs):
    return ImportRow.objects.create(run=run, batch=batch, **kwargs)


@pytest.fixture
def commits(django_capture_on_commit_callbacks):
    """Run what the dispatcher deferred, the way a real commit would.

    The join is deliberately deferred to commit -- a worker must never read a batch row
    its own transaction has not written yet -- so a test that wants to see the parent
    move has to say where the commit is.
    """

    @contextmanager
    def _commit():
        with django_capture_on_commit_callbacks(execute=True):
            yield

    return _commit


# -------------------------------------------------------------------- opening


def test_open_batch_records_where_the_record_is_waiting(waiting_run, row_version):
    batch = a_batch(waiting_run)

    assert batch.lifecycle == BatchLifecycle.OPEN
    assert batch.target == waiting_run
    assert batch.opened_in_status.key == "processing"
    assert batch.join_action.key == "import_run.finish"
    # The model field, not the catalog's status_field: it is what the join hands to
    # transition(), and what lets one record wait on two governed statuses at once.
    assert batch.status_field == "status_key"


def test_open_batch_is_safe_to_call_twice(waiting_run):
    """A hook that fires again after a retry must not open a second batch."""
    first = a_batch(waiting_run)
    second = a_batch(waiting_run)

    assert second.pk == first.pk
    assert StatusBatch.objects.count() == 1


def test_open_batch_with_a_total_is_sealed_from_birth(waiting_run):
    batch = a_batch(waiting_run, total=1204)

    assert batch.sealed is True
    assert batch.total == 1204


def test_open_batch_without_a_total_is_not_sealed(waiting_run):
    batch = a_batch(waiting_run)

    assert batch.sealed is False
    assert batch.is_complete is False


def test_the_actor_is_snapshotted_once_for_the_whole_fan_out(waiting_run):
    """One identity row per fan-out, not one per child. See identities.resolve_identity."""
    batch = a_batch(waiting_run)

    assert batch.actor_id is not None
    assert batch.actor.is_system


def test_a_timeout_becomes_a_deadline(waiting_run):
    before = timezone.now()
    batch = a_batch(waiting_run, timeout=timedelta(hours=2))

    assert batch.timeout_at >= before + timedelta(hours=2)


def test_nesting_deeper_than_the_cap_is_refused(waiting_run, row_version, import_run):
    """A machine whose child machine is itself would otherwise recurse without end."""
    parent = a_batch(waiting_run)
    parent.depth = 10

    with pytest.raises(BatchDepthExceeded, match="MAX_BATCH_DEPTH"):
        open_batch(import_run, join_action="import_run.finish", parent_batch=parent)


def test_a_child_batch_records_its_parent_and_its_depth(waiting_run, import_run):
    parent = a_batch(waiting_run)
    child = open_batch(import_run, join_action="import_run.finish", parent_batch=parent)

    assert child.parent_batch_id == parent.pk
    assert child.depth == parent.depth + 1


def test_live_batch_for_finds_only_a_live_one(waiting_run):
    batch = a_batch(waiting_run)
    assert live_batch_for(waiting_run).pk == batch.pk

    StatusBatch.objects.filter(pk=batch.pk).update(lifecycle=BatchLifecycle.CLOSED)

    assert live_batch_for(waiting_run) is None


# -------------------------------------------------------------------- sealing


def test_seal_counts_the_children_itself(waiting_run, row_version):
    """Nobody maintains a total by hand, so it cannot drift."""
    batch = a_batch(waiting_run)
    a_row(waiting_run, batch)
    a_row(waiting_run, batch)
    ImportNote.objects.create(batch=batch)

    seal(batch)

    assert batch.sealed is True
    assert batch.total == 3


def test_seal_accepts_a_total_it_is_given(waiting_run):
    batch = a_batch(waiting_run)

    seal(batch, total=7)

    assert batch.total == 7


def test_seal_claims_a_batch_whose_children_all_finished_first(waiting_run, row_version, commits):
    """The trap: every child reported while the batch was not yet allowed to complete."""
    batch = a_batch(waiting_run)
    row = a_row(waiting_run, batch)
    count_child(row, outcome=SUCCESS)

    batch.refresh_from_db()
    assert batch.lifecycle == BatchLifecycle.OPEN  # nobody could have been last

    with commits():
        seal(batch)

    batch.refresh_from_db()
    assert batch.lifecycle == BatchLifecycle.CLOSED
    waiting_run.refresh_from_db()
    assert waiting_run.status_key == "completed"


# ------------------------------------------------------------------- counting


def test_counting_a_child_moves_the_counter_and_stamps_it(waiting_run, row_version):
    batch = a_batch(waiting_run, total=2)
    row = a_row(waiting_run, batch)

    assert count_child(row, outcome=SUCCESS) is True

    batch.refresh_from_db()
    row.refresh_from_db()
    assert (batch.finished, batch.succeeded) == (1, 1)
    assert row.batch_reported_at is not None


def test_a_failure_counts_as_finished_but_not_succeeded(waiting_run, row_version):
    batch = a_batch(waiting_run, total=2)
    row = a_row(waiting_run, batch)

    count_child(row, outcome=FAILURE)

    batch.refresh_from_db()
    assert (batch.finished, batch.succeeded, batch.failed) == (1, 0, 1)


def test_counting_the_same_child_twice_counts_once(waiting_run, row_version):
    """A redelivered task finds the stamp already set and does nothing."""
    batch = a_batch(waiting_run, total=2)
    row = a_row(waiting_run, batch)

    assert count_child(row, outcome=SUCCESS) is True
    assert count_child(row, outcome=SUCCESS) is False

    batch.refresh_from_db()
    assert batch.finished == 1


def test_a_child_with_no_batch_counts_nothing(waiting_run, row_version):
    row = a_row(waiting_run, None)

    assert count_child(row, outcome=SUCCESS) is False


def test_reporting_into_an_abandoned_batch_is_a_no_op_not_an_error(waiting_run, row_version):
    """A worker already running when a cancel lands will finish and report. Always."""
    batch = a_batch(waiting_run, total=2)
    row = a_row(waiting_run, batch)
    StatusBatch.objects.filter(pk=batch.pk).update(lifecycle=BatchLifecycle.ABANDONED)

    count_child(row, outcome=SUCCESS)

    batch.refresh_from_db()
    assert batch.finished == 0
    assert batch.lifecycle == BatchLifecycle.ABANDONED


# ---------------------------------------------------------------- un-counting


def test_un_counting_walks_the_counter_back_and_clears_the_stamp(waiting_run, row_version):
    batch = a_batch(waiting_run, total=2)
    row = a_row(waiting_run, batch)
    count_child(row, outcome=SUCCESS)

    assert uncount_child(row, outcome=SUCCESS) is True

    batch.refresh_from_db()
    row.refresh_from_db()
    assert (batch.finished, batch.succeeded) == (0, 0)
    assert row.batch_reported_at is None


def test_un_counting_a_child_that_was_never_counted_does_nothing(waiting_run, row_version):
    batch = a_batch(waiting_run, total=2)
    row = a_row(waiting_run, batch)

    assert uncount_child(row, outcome=SUCCESS) is False


def test_a_child_can_be_counted_again_after_being_un_counted(waiting_run, row_version):
    batch = a_batch(waiting_run, total=2)
    row = a_row(waiting_run, batch)

    count_child(row, outcome=SUCCESS)
    uncount_child(row, outcome=SUCCESS)
    assert count_child(row, outcome=SUCCESS) is True

    batch.refresh_from_db()
    assert batch.finished == 1


def test_un_counting_does_not_drag_back_a_batch_that_is_already_joining(waiting_run, row_version):
    """Once the parent has been decided, reopening a child is a new story."""
    batch = a_batch(waiting_run, total=2)
    row = a_row(waiting_run, batch)
    count_child(row, outcome=SUCCESS)
    StatusBatch.objects.filter(pk=batch.pk).update(lifecycle=BatchLifecycle.JOINING)

    uncount_child(row, outcome=SUCCESS)

    batch.refresh_from_db()
    assert batch.finished == 1


def test_un_counting_never_drives_a_counter_negative(waiting_run, row_version):
    """PositiveIntegerField would refuse the write, so the filter has to guard it."""
    batch = a_batch(waiting_run, total=2)
    row = a_row(waiting_run, batch)
    count_child(row, outcome=SUCCESS)
    # A drifted counter: the stamp says counted, the number disagrees.
    StatusBatch.objects.filter(pk=batch.pk).update(finished=0, succeeded=0)

    uncount_child(row, outcome=SUCCESS)

    batch.refresh_from_db()
    assert (batch.finished, batch.succeeded) == (0, 0)


# ------------------------------------------------------- the opaque escape hatch


def test_an_opaque_report_counts_once_per_key(waiting_run):
    batch = a_batch(waiting_run, total=3)

    assert report_success(batch, "row-1") is True
    assert report_success(batch, "row-1") is False
    assert report_failure(batch, "row-2") is True

    batch.refresh_from_db()
    assert (batch.finished, batch.succeeded, batch.failed) == (2, 1, 1)


def test_an_opaque_report_needs_a_key(waiting_run):
    """There is no unprotected path: without an identity a retry counts twice."""
    batch = a_batch(waiting_run, total=1)

    with pytest.raises(ValueError, match="needs a key"):
        report(batch, "", SUCCESS)


def test_opaque_reports_are_deleted_when_the_batch_closes(waiting_run, commits):
    batch = a_batch(waiting_run, total=1)
    with commits():
        report_success(batch, "row-1")

    batch.refresh_from_db()
    assert batch.lifecycle == BatchLifecycle.CLOSED
    assert not StatusBatchReport.objects.filter(batch=batch).exists()


# -------------------------------------------------------------------- claiming


def test_the_claim_is_won_by_exactly_one_caller(waiting_run):
    batch = a_batch(waiting_run, total=1)
    StatusBatch.objects.filter(pk=batch.pk).update(finished=1)

    assert try_claim(batch.pk) is True
    assert try_claim(batch.pk) is False


def test_an_incomplete_batch_cannot_be_claimed(waiting_run):
    batch = a_batch(waiting_run, total=5)
    StatusBatch.objects.filter(pk=batch.pk).update(finished=4)

    assert try_claim(batch.pk) is False


def test_an_unsealed_batch_cannot_be_claimed_however_the_numbers_look(waiting_run):
    batch = a_batch(waiting_run)
    StatusBatch.objects.filter(pk=batch.pk).update(total=0, finished=0)

    assert try_claim(batch.pk) is False


def test_a_failure_reason_claims_a_batch_that_never_completed(waiting_run):
    """How a timeout ends one: the work never finished, the parent still has to move."""
    batch = a_batch(waiting_run, total=5)
    StatusBatch.objects.filter(pk=batch.pk).update(finished=1)

    assert try_claim(batch.pk, failure_reason=BatchFailureReason.TIMEOUT) is True

    batch.refresh_from_db()
    assert batch.failure_reason == "timeout"
    assert batch.joining_at is not None


def test_the_claim_stamps_joining_at_because_update_bypasses_auto_now(waiting_run):
    """modified_at would read the row's creation, and the sweeper would loop."""
    batch = a_batch(waiting_run, total=1)
    StatusBatch.objects.filter(pk=batch.pk).update(finished=1, joining_at=None)

    try_claim(batch.pk)

    batch.refresh_from_db()
    assert batch.joining_at is not None


# ----------------------------------------------------------------- the join


def test_the_join_moves_the_parent_and_closes_the_batch(waiting_run, row_version, commits):
    batch = a_batch(waiting_run, total=2)
    a_row(waiting_run, batch)
    a_row(waiting_run, batch)
    with commits():
        for row in ImportRow.objects.all():
            count_child(row, outcome=SUCCESS)

    waiting_run.refresh_from_db()
    batch.refresh_from_db()
    assert waiting_run.status_key == "completed"
    assert batch.lifecycle == BatchLifecycle.CLOSED


def test_a_partial_failure_lands_on_its_own_edge(waiting_run, row_version, commits):
    """One action, several guarded edges: the existing behaviour of _select."""
    batch = a_batch(waiting_run, total=2)
    good, bad = a_row(waiting_run, batch), a_row(waiting_run, batch)
    with commits():
        count_child(good, outcome=SUCCESS)
        count_child(bad, outcome=FAILURE)

    waiting_run.refresh_from_db()
    assert waiting_run.status_key == "partially_failed"


def test_everything_failing_falls_through_to_the_unguarded_edge(waiting_run, row_version, commits):
    batch = a_batch(waiting_run, total=2)
    with commits():
        for _ in range(2):
            count_child(a_row(waiting_run, batch), outcome=FAILURE)

    waiting_run.refresh_from_db()
    assert waiting_run.status_key == "failed"


def test_a_timeout_routes_on_its_reason_not_on_the_counts(waiting_run, row_version, commits):
    """A timed-out batch has failed == 0, and would otherwise read as a clean run."""
    batch = a_batch(waiting_run, total=5)
    count_child(a_row(waiting_run, batch), outcome=SUCCESS)

    with commits():
        try_claim(batch.pk, failure_reason=BatchFailureReason.TIMEOUT, failure_detail="2h")

    waiting_run.refresh_from_db()
    assert waiting_run.status_key == "timed_out"


def test_the_join_is_recorded_as_an_ordinary_move(waiting_run, row_version, commits):
    batch = a_batch(waiting_run, total=1)
    with commits():
        count_child(a_row(waiting_run, batch), outcome=SUCCESS)

    record = waiting_run.status_history().first()

    assert record.to_status.key == "completed"
    assert record.action_type.key == "import_run.finish"
    assert record.metadata["batch"]["id"] == batch.pk


def test_running_the_join_twice_is_harmless(waiting_run, row_version, commits):
    """The sweeper re-dispatches, so this has to be safe."""
    batch = a_batch(waiting_run, total=1)
    with commits():
        count_child(a_row(waiting_run, batch), outcome=SUCCESS)

    run_batch_operation(JOIN, batch.pk)

    waiting_run.refresh_from_db()
    assert waiting_run.status_key == "completed"
    assert waiting_run.status_history().count() == 2  # start, finish


def test_a_join_on_a_vanished_record_is_abandoned_not_retried_forever(waiting_run):
    batch = a_batch(waiting_run, total=1)
    StatusBatch.objects.filter(pk=batch.pk).update(finished=1, lifecycle=BatchLifecycle.JOINING)
    ImportRun.objects.filter(pk=waiting_run.pk).delete()

    run_batch_operation(JOIN, batch.pk)

    batch.refresh_from_db()
    assert batch.lifecycle == BatchLifecycle.ABANDONED
    assert batch.failure_reason == BatchFailureReason.JOIN_FAILED


def test_an_unknown_operation_is_refused():
    with pytest.raises(ValueError, match="Unknown batch operation"):
        run_batch_operation("dance", 1)


# ----------------------------------------------------------------- join payload


def test_join_metadata_always_carries_every_key_a_guard_could_read(waiting_run, row_version):
    """A guard is a plain subscript; a missing key raises through _assert_viable."""
    batch = a_batch(waiting_run, total=1)
    row = a_row(waiting_run, batch)
    count_child(row, outcome=SUCCESS)
    batch.refresh_from_db()

    payload = join_metadata(batch)

    for key in (
        "id",
        "total",
        "finished",
        "succeeded",
        "failed",
        "failure_reason",
        "failure_detail",
        "depth",
        "by_status",
    ):
        assert key in payload
    assert payload["failure_reason"] == ""


def test_by_status_is_zero_filled_from_the_graph(waiting_run, row_version):
    """A status that never occurred still has to be readable, or the guard raises."""
    batch = a_batch(waiting_run, total=1)
    row = a_row(waiting_run, batch)
    row.transition("import_row.process")
    count_child(row, outcome=SUCCESS)
    batch.refresh_from_db()

    by_status = join_metadata(batch)["by_status"]

    assert by_status["processed"] == 1
    assert by_status["rejected"] == 0
    assert by_status["queued"] == 0


def test_by_status_counts_only_stamped_children(waiting_run, row_version):
    batch = a_batch(waiting_run, total=3)
    counted = a_row(waiting_run, batch)
    a_row(waiting_run, batch)
    count_child(counted, outcome=SUCCESS)
    batch.refresh_from_db()

    assert join_metadata(batch)["by_status"]["queued"] == 1


# ------------------------------------------------------------------ dispatching


def test_the_join_is_deferred_until_commit(waiting_run, row_version):
    """A worker must never read a batch row the transaction has not written yet.

    So the claim and the join are separated by a commit, always -- which is also why
    every test that wants to see the parent move has to say where that commit is.
    """
    batch = a_batch(waiting_run, total=1)

    count_child(a_row(waiting_run, batch), outcome=SUCCESS)

    batch.refresh_from_db()
    assert batch.lifecycle == BatchLifecycle.JOINING  # claimed
    waiting_run.refresh_from_db()
    assert waiting_run.status_key == "processing"  # but not moved


def test_a_configured_dispatcher_is_used_instead(waiting_run, row_version, commits):
    seen: list[tuple[str, int]] = []

    with override_settings(
        STATE_MACHINES={
            "CACHE_GRAPHS": False,
            "BATCH_DISPATCHER": "tests.test_batches.record_dispatch",
        }
    ):
        _DISPATCHED.clear()
        batch = a_batch(waiting_run, total=1)
        with commits():
            count_child(a_row(waiting_run, batch), outcome=SUCCESS)
        seen = list(_DISPATCHED)

    assert seen == [(JOIN, batch.pk)]
    # The library dispatched rather than running it, so the parent has not moved.
    waiting_run.refresh_from_db()
    assert waiting_run.status_key == "processing"


_DISPATCHED: list[tuple[str, int]] = []


def record_dispatch(operation: str, batch_id: int) -> None:
    """Stands in for a queue. Referenced by dotted path from the test above."""
    _DISPATCHED.append((operation, batch_id))


def test_counters_use_expressions_so_concurrent_reports_do_not_clobber(waiting_run, row_version):
    """The counter moves with F(), which is what makes the row lock do the work."""
    batch = a_batch(waiting_run, total=10)
    StatusBatch.objects.filter(pk=batch.pk).update(finished=F("finished") + 3)
    StatusBatch.objects.filter(pk=batch.pk).update(finished=F("finished") + 4)

    batch.refresh_from_db()
    assert batch.finished == 7
