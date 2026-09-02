"""The batch tables: their invariants, their derived values, and their queryset."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.db.utils import DataError

from vinta_state_machines.conf import batch_model_path, get_setting
from vinta_state_machines.enums import BatchFailureReason, BatchLifecycle
from vinta_state_machines.identities import resolve_identity
from vinta_state_machines.models import (
    ActionType,
    StatusBatch,
    StatusBatchReport,
    StatusDefinition,
)

pytestmark = pytest.mark.django_db


def make_batch(risk, risk_version, **overrides):
    """A batch on ``risk``, opened in whatever state it is currently sitting on."""
    defaults = {
        "target_type": ContentType.objects.get_for_model(risk),
        "target_id": str(risk.pk),
        "status_field": "status",
        "opened_in_status": StatusDefinition.objects.get(
            entity_type="risk", status_field="status", key=risk.status_key
        ),
        "state_machine_version": risk_version,
        "join_action": ActionType.objects.get(key="risk.assess"),
        "actor": resolve_identity(None),
    }
    return StatusBatch.objects.create(**{**defaults, **overrides})


# ------------------------------------------------------------------- invariants


def test_only_one_live_batch_per_record_and_field(risk, risk_version):
    """The constraint that makes open_batch safe to call twice."""
    make_batch(risk, risk_version)

    with pytest.raises(IntegrityError), transaction.atomic():
        make_batch(risk, risk_version)


def test_a_closed_batch_frees_the_record_for_another(risk, risk_version):
    """Reopening after a retry is ordinary, so only *live* batches are exclusive."""
    first = make_batch(risk, risk_version)
    first.lifecycle = BatchLifecycle.CLOSED
    first.save(update_fields=["lifecycle"])

    second = make_batch(risk, risk_version)

    assert second.pk != first.pk


def test_the_same_record_may_wait_on_two_different_status_fields(risk, risk_version):
    """Exclusivity is per status field, because the two statuses are independent."""
    make_batch(risk, risk_version)
    other = make_batch(risk, risk_version, status_field="engagement_status")

    assert other.pk


def test_a_joining_batch_still_owns_the_record(risk, risk_version):
    first = make_batch(risk, risk_version, lifecycle=BatchLifecycle.JOINING)
    assert first.is_live

    with pytest.raises(IntegrityError), transaction.atomic():
        make_batch(risk, risk_version)


def test_a_report_key_is_unique_within_its_batch(risk, risk_version):
    """The unique index *is* the double-count protection for opaque work."""
    batch = make_batch(risk, risk_version)
    StatusBatchReport.objects.create(batch=batch, key="row-1", outcome="success")

    with pytest.raises(IntegrityError), transaction.atomic():
        StatusBatchReport.objects.create(batch=batch, key="row-1", outcome="failure")


def test_the_same_report_key_may_appear_in_two_batches(risk, risk_version):
    batch = make_batch(risk, risk_version)
    batch.lifecycle = BatchLifecycle.CLOSED
    batch.save(update_fields=["lifecycle"])
    later = make_batch(risk, risk_version)

    StatusBatchReport.objects.create(batch=batch, key="row-1", outcome="success")
    StatusBatchReport.objects.create(batch=later, key="row-1", outcome="success")

    assert StatusBatchReport.objects.filter(key="row-1").count() == 2


def test_closing_a_batch_takes_its_reports_with_it(risk, risk_version):
    """Reports do not outlive their batch, so the table only holds work in flight."""
    batch = make_batch(risk, risk_version)
    StatusBatchReport.objects.create(batch=batch, key="row-1", outcome="success")

    batch.delete()

    assert not StatusBatchReport.objects.exists()


# ---------------------------------------------------------------- derived values


@pytest.mark.parametrize(
    ("sealed", "total", "finished", "expected"),
    [
        (True, 3, 3, True),
        (True, 3, 2, False),
        # Unsealed is never complete, however the numbers look. This is the whole
        # point of sealing: children can still be added.
        (False, 3, 3, False),
        (False, 0, 0, False),
        (True, 0, 0, True),
    ],
)
def test_is_complete(risk, risk_version, sealed, total, finished, expected):
    batch = make_batch(risk, risk_version, sealed=sealed, total=total, finished=finished)
    assert batch.is_complete is expected


def test_failed_is_derived_not_stored(risk, risk_version):
    batch = make_batch(risk, risk_version, total=10, finished=10, succeeded=7)
    assert batch.failed == 3


def test_failed_never_goes_negative(risk, risk_version):
    """A drifted counter should read oddly, not produce a negative count."""
    batch = make_batch(risk, risk_version, finished=1, succeeded=4)
    assert batch.failed == 0


@pytest.mark.parametrize(
    ("total", "finished", "expected"),
    [(0, 0, 1.0), (4, 1, 0.25), (4, 4, 1.0), (4, 9, 1.0)],
)
def test_progress(risk, risk_version, total, finished, expected):
    batch = make_batch(risk, risk_version, total=total, finished=finished)
    assert batch.progress == expected


def test_counters_refuse_to_go_negative_in_the_database(risk, risk_version):
    """PositiveIntegerField is why the un-count path has to guard its filter."""
    batch = make_batch(risk, risk_version, finished=0)

    with pytest.raises((IntegrityError, DataError, ValueError)), transaction.atomic():
        StatusBatch.objects.filter(pk=batch.pk).update(finished=-1)


# -------------------------------------------------------------------- queryset


def test_for_object_finds_every_batch_of_one_record(risk, risk_version):
    batch = make_batch(risk, risk_version)
    batch.lifecycle = BatchLifecycle.CLOSED
    batch.save(update_fields=["lifecycle"])
    make_batch(risk, risk_version)

    assert StatusBatch.objects.for_object(risk).count() == 2
    assert StatusBatch.objects.for_object(risk, status_field="engagement_status").count() == 0


def test_live_and_complete_are_independent_questions(risk, risk_version):
    """A complete batch nobody has claimed is exactly what the sweeper looks for."""
    batch = make_batch(risk, risk_version, sealed=True, total=2, finished=2)

    assert StatusBatch.objects.live().filter(pk=batch.pk).exists()
    assert StatusBatch.objects.complete().filter(pk=batch.pk).exists()
    assert StatusBatch.objects.open().filter(pk=batch.pk).exists()
    assert not StatusBatch.objects.joining().filter(pk=batch.pk).exists()


def test_with_progress_orders_least_done_first(risk, risk_version):
    """What a changelist wants when somebody is looking for the stuck one."""
    behind = make_batch(risk, risk_version, total=10, finished=1)
    behind.lifecycle = BatchLifecycle.CLOSED
    behind.save(update_fields=["lifecycle"])
    ahead = make_batch(risk, risk_version, total=10, finished=9)

    ordered = list(StatusBatch.objects.with_progress().order_by("progress_ratio"))

    assert [item.pk for item in ordered] == [behind.pk, ahead.pk]
    assert ordered[0].progress_ratio == pytest.approx(0.1)


def test_with_progress_survives_an_empty_batch(risk, risk_version):
    """Dividing by total would be a ZeroDivisionError in SQL as surely as in Python."""
    batch = make_batch(risk, risk_version, total=0, finished=0)

    annotated = StatusBatch.objects.with_progress().get(pk=batch.pk)

    assert annotated.progress_ratio == 1.0


# --------------------------------------------------------------------- settings


def test_the_batch_model_is_swappable_and_defaults_to_ours():
    assert batch_model_path() == "state_machines.StatusBatch"
    assert StatusBatch._meta.swappable == "STATE_MACHINES_BATCH_MODEL"


def test_new_settings_have_defaults():
    assert get_setting("BATCH_DISPATCHER") is None
    assert get_setting("MAX_BATCH_DEPTH") == 10
    assert get_setting("BATCH_JOIN_RETRY_AFTER") == timedelta(minutes=5)


def test_failure_reason_is_not_constrained_to_our_values(risk, risk_version):
    """A project records reasons of its own without a migration."""
    batch = make_batch(risk, risk_version, failure_reason="quota_exhausted")
    batch.full_clean(exclude=["target_id", "metadata"])

    assert batch.failure_reason == "quota_exhausted"
    assert BatchFailureReason.TIMEOUT.value == "timeout"


def test_lifecycle_live_names_both_owning_states():
    assert set(BatchLifecycle.live()) == {"open", "joining"}
