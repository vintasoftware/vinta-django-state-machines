"""The child half of a batch: the pair of fields, its checks, and member discovery."""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.test.utils import isolate_apps

from tests.test_batch_models import make_batch
from tests.testapp.models import ImportNote, ImportRow, Risk
from vinta_state_machines.checks import _check_batch_field, check_batch_fields
from vinta_state_machines.fields import (
    BatchReportedAtField,
    StatusBatchField,
    batch_fields_of,
    batch_member_relations,
    default_reported_at_field_name,
    get_batch_field_config,
)
from vinta_state_machines.models import StatusBatch

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------ the pair


def test_the_companion_name_is_derived_from_the_batch_field():
    assert default_reported_at_field_name("batch") == "batch_reported_at"
    assert default_reported_at_field_name("etl_batch") == "etl_batch_reported_at"


def test_the_pair_is_two_ordinary_concrete_fields():
    """The whole point: migrations and select_related behave as if hand written."""
    batch = ImportRow._meta.get_field("batch")
    stamp = ImportRow._meta.get_field("batch_reported_at")

    assert isinstance(batch, models.ForeignKey)
    assert isinstance(stamp, models.DateTimeField)
    assert batch.null and stamp.null


def test_the_stamp_is_not_editable():
    """It is bookkeeping. Nobody should be offered it in a form."""
    assert ImportRow._meta.get_field("batch_reported_at").editable is False


def test_deleting_a_batch_does_not_delete_its_children():
    """A batch is bookkeeping about work; the work outlives the bookkeeping."""
    assert ImportRow._meta.get_field("batch").remote_field.on_delete is models.SET_NULL


def test_the_reverse_accessor_is_scoped_per_child_model():
    """Two models counted by one batch would clash on a single shared accessor."""
    assert ImportRow._meta.get_field("batch").remote_field.related_name == (
        "testapp_importrow_members"
    )
    assert ImportNote._meta.get_field("batch").remote_field.related_name == (
        "testapp_importnote_members"
    )


def test_the_pair_can_be_named_something_else():
    from tests.testapp.models import RenamedBatchChild

    config = get_batch_field_config(RenamedBatchChild)

    assert config.field_name == "etl"
    assert config.reported_at_field == "etl_counted_at"


def test_deconstruct_round_trips_an_explicit_companion():
    field = StatusBatchField(reported_at_field="etl_counted_at")
    _name, path, _args, kwargs = field.deconstruct()

    assert path.endswith("StatusBatchField")
    assert kwargs["reported_at_field"] == "etl_counted_at"
    # Defaults stay implicit, so a migration does not record what it does not need.
    assert "to" not in kwargs
    assert "on_delete" not in kwargs


# ------------------------------------------------------------------- lookup


def test_the_field_need_not_be_named_when_there_is_only_one():
    assert get_batch_field_config(ImportRow).field_name == "batch"


def test_a_model_with_no_batch_field_says_so():
    with pytest.raises(ImproperlyConfigured, match="declares 0 batch fields"):
        get_batch_field_config(Risk)


def test_naming_a_field_that_is_not_a_batch_field_says_so():
    with pytest.raises(ImproperlyConfigured, match="not a StatusBatchField"):
        get_batch_field_config(ImportRow, "payload")


def test_naming_a_field_that_does_not_exist_lists_the_ones_that_do():
    with pytest.raises(ImproperlyConfigured, match="Batch fields on this model: batch"):
        get_batch_field_config(ImportRow, "nope")


def test_batch_fields_of_finds_them():
    assert [f.name for f in batch_fields_of(ImportRow)] == ["batch"]
    assert batch_fields_of(Risk) == []


# -------------------------------------------------------- member discovery


def test_a_batch_finds_its_children_without_being_told_what_they_are():
    relations = batch_member_relations(StatusBatch)
    models_found = {relation.related_model for relation in relations}

    assert ImportRow in models_found
    assert ImportNote in models_found
    assert Risk not in models_found


def test_count_members_spans_every_kind_of_child(risk, risk_version, import_run):
    """One batch may count two different models, and nobody configured that."""
    batch = make_batch(risk, risk_version)
    ImportRow.objects.create(run=import_run, batch=batch)
    ImportRow.objects.create(run=import_run, batch=batch)
    ImportNote.objects.create(batch=batch, body="a note")

    assert batch.count_members() == 3


def test_count_stamped_members_counts_only_what_carries_a_stamp(risk, risk_version, import_run):
    """The authority the sweeper repairs a drifted counter against."""
    from django.utils import timezone

    batch = make_batch(risk, risk_version)
    ImportRow.objects.create(run=import_run, batch=batch, batch_reported_at=timezone.now())
    ImportRow.objects.create(run=import_run, batch=batch)
    ImportNote.objects.create(batch=batch, batch_reported_at=timezone.now())

    assert batch.count_members() == 3
    assert batch.count_stamped_members() == 2


def test_member_querysets_ignore_other_batches(risk, risk_version, import_run):
    batch = make_batch(risk, risk_version)
    ImportRow.objects.create(run=import_run, batch=batch)
    ImportRow.objects.create(run=import_run, batch=None)

    assert batch.count_members() == 1


# -------------------------------------------------------------------- checks


def test_the_checks_pass_on_a_correctly_declared_pair():
    assert check_batch_fields() == []


@isolate_apps("tests.testapp")
def test_a_missing_companion_is_an_error():
    """The mistake this check exists for: half the pair declared."""

    class MissingStamp(models.Model):
        batch = StatusBatchField()

        class Meta:
            app_label = "testapp"

    issues = _check_batch_field(MissingStamp, MissingStamp._meta.get_field("batch"))

    assert [issue.id for issue in issues] == ["state_machines.E009"]
    assert "BatchReportedAtField()" in issues[0].hint


@isolate_apps("tests.testapp")
def test_a_companion_of_the_wrong_type_is_an_error():
    class WrongStampType(models.Model):
        batch = StatusBatchField()
        batch_reported_at = models.CharField(max_length=10, blank=True)

        class Meta:
            app_label = "testapp"

    issues = _check_batch_field(WrongStampType, WrongStampType._meta.get_field("batch"))

    assert [issue.id for issue in issues] == ["state_machines.E010"]


@isolate_apps("tests.testapp")
def test_a_companion_that_cannot_be_null_is_an_error():
    """NULL is the value that means "not counted", and the idempotent UPDATE needs it."""

    class NotNullableStamp(models.Model):
        batch = StatusBatchField()
        batch_reported_at = BatchReportedAtField(null=False)

        class Meta:
            app_label = "testapp"

    issues = _check_batch_field(NotNullableStamp, NotNullableStamp._meta.get_field("batch"))

    assert [issue.id for issue in issues] == ["state_machines.E011"]
    assert "not counted" in issues[0].hint


@isolate_apps("tests.testapp")
def test_an_explicitly_named_companion_is_looked_up_by_that_name():
    class Explicit(models.Model):
        etl = StatusBatchField(reported_at_field="etl_counted_at")
        etl_counted_at = BatchReportedAtField()

        class Meta:
            app_label = "testapp"

    assert _check_batch_field(Explicit, Explicit._meta.get_field("etl")) == []
