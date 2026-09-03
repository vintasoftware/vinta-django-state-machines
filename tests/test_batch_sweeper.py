"""The sweeper: the four ways a batch gets unstuck."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from tests.testapp.models import ImportNote, ImportRow
from vinta_state_machines.batch_effects import wire_batch_reporting
from vinta_state_machines.batches import SUCCESS, count_child, open_batch, recount
from vinta_state_machines.enums import BatchFailureReason, BatchLifecycle
from vinta_state_machines.models import StatusBatch, StatusBatchReport
from vinta_state_machines.sweeper import sweep

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


# ------------------------------------------------------------------ recounting


def test_recount_reads_the_stamps_not_the_counter(waiting_run, row_version):
    wire_batch_reporting(row_version, {"processed": "success", "rejected": "failure"})
    batch = a_batch(waiting_run, total=3)
    for _ in range(2):
        row = ImportRow.objects.create(run=waiting_run, batch=batch)
        row.status_key = "processed"
        row.save(update_fields=["status_key"])
        row.batch_reported_at = timezone.now()
        row.save(update_fields=["batch_reported_at"])

    assert recount(batch) == (2, 2)


def test_recount_reconstructs_successes_from_the_report_bindings(waiting_run, row_version):
    """A stamp records *that* a child was counted, never how it went."""
    wire_batch_reporting(row_version, {"processed": "success", "rejected": "failure"})
    batch = a_batch(waiting_run, total=2)
    for status in ("processed", "rejected"):
        row = ImportRow.objects.create(run=waiting_run, batch=batch)
        row.status_key = status
        row.batch_reported_at = timezone.now()
        row.save(update_fields=["status_key", "batch_reported_at"])

    assert recount(batch) == (2, 1)


def test_recount_counts_a_child_with_no_governed_status_toward_completeness_only(
    waiting_run,
):
    batch = a_batch(waiting_run, total=1)
    ImportNote.objects.create(batch=batch, batch_reported_at=timezone.now())

    assert recount(batch) == (1, 0)


def test_recount_includes_opaque_reports(waiting_run):
    batch = a_batch(waiting_run, total=3)
    StatusBatchReport.objects.create(batch=batch, key="a", outcome="success")
    StatusBatchReport.objects.create(batch=batch, key="b", outcome="failure")

    assert recount(batch) == (2, 1)


# -------------------------------------------------------------------- repair


def test_the_sweeper_repairs_a_counter_that_lost_a_write(waiting_run, row_version):
    """A worker stamped its child and died before incrementing."""
    wire_batch_reporting(row_version, {"processed": "success", "rejected": "failure"})
    batch = a_batch(waiting_run, total=3)
    row = ImportRow.objects.create(run=waiting_run, batch=batch)
    row.status_key = "processed"
    row.batch_reported_at = timezone.now()
    row.save(update_fields=["status_key", "batch_reported_at"])

    report = sweep()

    batch.refresh_from_db()
    assert report.repaired == 1
    assert (batch.finished, batch.succeeded) == (1, 1)


def test_repair_moves_both_numbers_together(waiting_run, row_version):
    """Repairing finished alone would read as a failure and route down the wrong edge."""
    wire_batch_reporting(row_version, {"processed": "success", "rejected": "failure"})
    batch = a_batch(waiting_run, total=5)
    for _ in range(3):
        row = ImportRow.objects.create(run=waiting_run, batch=batch)
        row.status_key = "processed"
        row.batch_reported_at = timezone.now()
        row.save(update_fields=["status_key", "batch_reported_at"])

    sweep()

    batch.refresh_from_db()
    assert batch.finished == batch.succeeded == 3
    assert batch.failed == 0


def test_a_correct_counter_is_left_alone(waiting_run, row_version):
    wire_batch_reporting(row_version, {"processed": "success", "rejected": "failure"})
    batch = a_batch(waiting_run, total=3)
    row = ImportRow.objects.create(run=waiting_run, batch=batch)
    row.transition("import_row.process")
    count_child(row, outcome=SUCCESS)

    assert sweep().repaired == 0


def test_repair_leaves_a_claimed_batch_alone(waiting_run, row_version):
    """Its numbers have already been read by the join; rewriting them under it is worse."""
    batch = a_batch(waiting_run, total=3)
    ImportRow.objects.create(run=waiting_run, batch=batch, batch_reported_at=timezone.now())
    StatusBatch.objects.filter(pk=batch.pk).update(lifecycle=BatchLifecycle.JOINING)

    assert sweep().repaired == 0


# ------------------------------------------------------------- claiming late


def stamped_row(run, batch, status="processed"):
    """A child that really finished: stamped, and sitting on a finished state."""
    row = ImportRow.objects.create(run=run, batch=batch)
    row.status_key = status
    row.batch_reported_at = timezone.now()
    row.save(update_fields=["status_key", "batch_reported_at"])
    return row


def test_the_sweeper_claims_a_complete_batch_nobody_claimed(waiting_run, row_version, commits):
    """The gap: the count committed, then the process died before the claim."""
    wire_batch_reporting(row_version, {"processed": "success", "rejected": "failure"})
    batch = a_batch(waiting_run, total=1)
    stamped_row(waiting_run, batch)
    StatusBatch.objects.filter(pk=batch.pk).update(finished=1, succeeded=1)

    with commits():
        report = sweep()

    batch.refresh_from_db()
    waiting_run.refresh_from_db()
    assert report.claimed == 1
    assert batch.lifecycle == BatchLifecycle.CLOSED
    assert waiting_run.status_key == "completed"


def test_an_unsealed_batch_is_never_claimed_by_the_sweeper(waiting_run):
    batch = a_batch(waiting_run)
    StatusBatch.objects.filter(pk=batch.pk).update(total=0, finished=0)

    assert sweep().claimed == 0


def test_an_incomplete_batch_is_not_claimed(waiting_run):
    batch = a_batch(waiting_run, total=4)
    StatusBatch.objects.filter(pk=batch.pk).update(finished=2, succeeded=2)

    assert sweep().claimed == 0


# --------------------------------------------------------------- re-dispatch


def test_a_join_that_never_finished_is_dispatched_again(waiting_run):
    batch = a_batch(waiting_run, total=1)
    StatusBatch.objects.filter(pk=batch.pk).update(
        lifecycle=BatchLifecycle.JOINING,
        joining_at=timezone.now() - timedelta(minutes=30),
    )

    assert sweep().redispatched == 1


def test_a_join_still_within_its_window_is_left_alone(waiting_run):
    batch = a_batch(waiting_run, total=1)
    StatusBatch.objects.filter(pk=batch.pk).update(
        lifecycle=BatchLifecycle.JOINING, joining_at=timezone.now()
    )

    assert sweep().redispatched == 0


def test_a_batch_may_override_the_retry_window(waiting_run):
    """Null means the setting; a column means this one differs."""
    batch = a_batch(waiting_run, total=1)
    StatusBatch.objects.filter(pk=batch.pk).update(
        lifecycle=BatchLifecycle.JOINING,
        joining_at=timezone.now() - timedelta(minutes=2),
        join_retry_after=timedelta(minutes=1),
    )

    assert sweep().redispatched == 1


def test_an_override_can_also_make_the_window_longer(waiting_run):
    batch = a_batch(waiting_run, total=1)
    StatusBatch.objects.filter(pk=batch.pk).update(
        lifecycle=BatchLifecycle.JOINING,
        joining_at=timezone.now() - timedelta(minutes=30),
        join_retry_after=timedelta(hours=4),
    )

    assert sweep().redispatched == 0


@override_settings(
    STATE_MACHINES={"CACHE_GRAPHS": False, "BATCH_JOIN_RETRY_AFTER": timedelta(seconds=1)}
)
def test_the_default_window_comes_from_settings(waiting_run):
    batch = a_batch(waiting_run, total=1)
    StatusBatch.objects.filter(pk=batch.pk).update(
        lifecycle=BatchLifecycle.JOINING,
        joining_at=timezone.now() - timedelta(seconds=30),
    )

    assert sweep().redispatched == 1


def test_a_batch_claimed_but_never_stamped_is_not_redispatched_forever(waiting_run):
    """joining_at is null only for rows written before this existed. Skip, do not loop."""
    batch = a_batch(waiting_run, total=1)
    StatusBatch.objects.filter(pk=batch.pk).update(
        lifecycle=BatchLifecycle.JOINING, joining_at=None
    )

    assert sweep().redispatched == 0


# ------------------------------------------------------------------- timeouts


def test_a_batch_past_its_deadline_is_claimed_with_a_reason(waiting_run, row_version, commits):
    batch = a_batch(waiting_run, total=10, timeout=timedelta(hours=1))
    StatusBatch.objects.filter(pk=batch.pk).update(
        timeout_at=timezone.now() - timedelta(minutes=1)
    )

    with commits():
        report = sweep()

    batch.refresh_from_db()
    waiting_run.refresh_from_db()
    assert report.timed_out == 1
    assert batch.failure_reason == BatchFailureReason.TIMEOUT
    assert batch.failure_detail
    # Routed on the reason, not the counts: an unfinished batch has failed == 0 and
    # would otherwise read as a clean run.
    assert waiting_run.status_key == "timed_out"


def test_a_batch_with_no_deadline_never_times_out(waiting_run):
    a_batch(waiting_run, total=10)

    assert sweep().timed_out == 0


def test_a_deadline_in_the_future_is_left_alone(waiting_run):
    a_batch(waiting_run, total=10, timeout=timedelta(hours=1))

    assert sweep().timed_out == 0


def test_a_batch_that_completed_before_its_deadline_closes_normally(
    waiting_run, row_version, commits
):
    wire_batch_reporting(row_version, {"processed": "success", "rejected": "failure"})
    batch = a_batch(waiting_run, total=1, timeout=timedelta(hours=1))
    stamped_row(waiting_run, batch)
    StatusBatch.objects.filter(pk=batch.pk).update(finished=1, succeeded=1)

    with commits():
        sweep()

    batch.refresh_from_db()
    waiting_run.refresh_from_db()
    assert batch.failure_reason == ""
    assert waiting_run.status_key == "completed"


# ------------------------------------------------------------------- command


def test_the_command_reports_what_it_did(waiting_run, row_version):
    batch = a_batch(waiting_run, total=3)
    ImportRow.objects.create(run=waiting_run, batch=batch, batch_reported_at=timezone.now())
    out = StringIO()

    call_command("close_status_batches", stdout=out)

    assert "repaired 1" in out.getvalue()


def test_the_command_stays_quiet_when_asked_and_there_is_nothing_to_do(waiting_run):
    out = StringIO()

    call_command("close_status_batches", "--quiet", stdout=out)

    assert out.getvalue() == ""


def test_the_command_still_speaks_when_idle_by_default():
    out = StringIO()

    call_command("close_status_batches", stdout=out)

    assert "repaired 0" in out.getvalue()


def test_sweeping_twice_changes_nothing_the_second_time(waiting_run, row_version):
    wire_batch_reporting(row_version, {"processed": "success", "rejected": "failure"})
    batch = a_batch(waiting_run, total=5)
    row = ImportRow.objects.create(run=waiting_run, batch=batch)
    row.status_key = "processed"
    row.batch_reported_at = timezone.now()
    row.save(update_fields=["status_key", "batch_reported_at"])

    assert sweep().repaired == 1
    assert sweep().repaired == 0


def test_repair_runs_before_the_claim_so_a_phantom_count_cannot_close_a_batch(
    waiting_run, row_version, commits
):
    """A counter nobody's stamps support is wrong, and repair is what says so.

    The order of the passes carries this: a batch whose number was inflated -- by a bug,
    a bad migration, somebody in a shell -- is corrected back down before the claim pass
    ever looks at it, so it cannot move its parent on work that never happened.
    """
    batch = a_batch(waiting_run, total=1)
    StatusBatch.objects.filter(pk=batch.pk).update(finished=1, succeeded=1)

    with commits():
        report = sweep()

    batch.refresh_from_db()
    waiting_run.refresh_from_db()
    assert report.repaired == 1
    assert report.claimed == 0
    assert batch.finished == 0
    assert waiting_run.status_key == "processing"
