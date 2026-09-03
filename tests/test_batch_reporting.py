"""Reading progress: the API the library ships instead of a refresh loop."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from tests.testapp.models import ImportRow
from vinta_state_machines.batches import abandon, open_batch
from vinta_state_machines.enums import BatchFailureReason, BatchLifecycle
from vinta_state_machines.models import StatusBatch
from vinta_state_machines.reporting import (
    batch_tree,
    current_batch,
    describe,
    progress_of,
    tree_of,
)

pytestmark = pytest.mark.django_db


def a_batch(run, **kwargs):
    kwargs.setdefault("join_action", "import_run.finish")
    return open_batch(run, **kwargs)


# ------------------------------------------------------------------- progress


def test_a_record_that_never_fanned_out_has_no_progress(waiting_run):
    assert progress_of(waiting_run) is None
    assert batch_tree(waiting_run) is None


def test_progress_reports_the_live_batch(waiting_run):
    batch = a_batch(waiting_run, total=4)
    StatusBatch.objects.filter(pk=batch.pk).update(finished=1, succeeded=1)

    progress = progress_of(waiting_run)

    assert progress["total"] == 4
    assert progress["finished"] == 1
    assert progress["progress"] == 0.25
    assert progress["lifecycle"] == BatchLifecycle.OPEN


def test_progress_falls_back_to_the_last_run_once_it_is_over(waiting_run):
    """ "What happened to the last import" is what somebody opening a finished one asks."""
    batch = a_batch(waiting_run, total=1)
    abandon(batch, reason=BatchFailureReason.CANCELLED)

    progress = progress_of(waiting_run)

    assert progress["lifecycle"] == BatchLifecycle.ABANDONED
    assert progress["failure_reason"] == "cancelled"


def test_a_live_batch_wins_over_an_older_one(waiting_run):
    old = a_batch(waiting_run, total=1)
    abandon(old, reason=BatchFailureReason.CANCELLED)
    live = a_batch(waiting_run, total=9)

    assert current_batch(waiting_run).pk == live.pk


# ---------------------------------------------------------------- serialising


def test_describe_is_plain_json_serialisable_data(waiting_run):
    """No model instances to leak into a template, or into a JsonResponse."""
    batch = a_batch(waiting_run, total=2, timeout=timedelta(hours=1))

    payload = describe(batch)

    json.dumps(payload)  # would raise if anything in here were a model or a datetime
    assert payload["target"] == "nightly"
    assert payload["opened_in_status"] == "processing"
    assert payload["children"] == []


def test_describe_survives_a_target_that_has_been_deleted(waiting_run):
    from tests.testapp.models import ImportRun

    batch = a_batch(waiting_run, total=1)
    ImportRun.objects.filter(pk=waiting_run.pk).delete()

    payload = describe(StatusBatch.objects.get(pk=batch.pk))

    assert payload["target"] == ""
    assert payload["target_id"] == str(waiting_run.pk)


# --------------------------------------------------------------------- trees


def test_a_tree_of_one_batch_is_one_node(waiting_run):
    a_batch(waiting_run, total=1)

    tree = batch_tree(waiting_run)

    assert tree["children"] == []


def test_a_nested_run_nests(waiting_run, row_version):
    parent = a_batch(waiting_run, total=1)
    row = ImportRow.objects.create(run=waiting_run, batch=parent)
    open_batch(row, join_action="import_row.process", parent_batch=parent)

    tree = batch_tree(waiting_run)

    assert len(tree["children"]) == 1
    assert tree["children"][0]["depth"] == 1


def test_the_tree_is_a_tree_of_batches_not_of_children(waiting_run, row_version):
    """Ninety thousand children draw as one node, which is what makes this affordable."""
    parent = a_batch(waiting_run, total=90000)
    for _ in range(20):
        ImportRow.objects.create(run=waiting_run, batch=parent)

    tree = batch_tree(waiting_run)

    assert tree["children"] == []
    assert tree["total"] == 90000


def test_the_tree_goes_deeper_than_one_level(waiting_run, row_version, run_version):
    from tests.testapp.models import ImportRun

    parent = a_batch(waiting_run, total=1)
    row = ImportRow.objects.create(run=waiting_run, batch=parent)
    middle = open_batch(row, join_action="import_row.process", parent_batch=parent)
    grandchild = ImportRun.objects.create(label="nested")
    grandchild.transition("import_run.start")
    open_batch(grandchild, join_action="import_run.finish", parent_batch=middle)

    tree = batch_tree(waiting_run)

    assert tree["children"][0]["children"][0]["depth"] == 2


def test_the_walk_is_bounded_and_does_not_loop(waiting_run, row_version):
    """A cycle in parent_batch would otherwise be a walk with no end."""
    parent = a_batch(waiting_run, total=1)
    row = ImportRow.objects.create(run=waiting_run, batch=parent)
    child = open_batch(row, join_action="import_row.process", parent_batch=parent)
    StatusBatch.objects.filter(pk=parent.pk).update(parent_batch=child)

    tree = tree_of(parent)

    assert tree["id"] == parent.pk


def test_a_sibling_batch_of_another_record_is_not_in_the_tree(
    waiting_run, row_version, import_run
):
    a_batch(waiting_run, total=1)
    open_batch(import_run, join_action="import_run.finish")

    assert batch_tree(waiting_run)["children"] == []


# --------------------------------------------------------------------- admin


@pytest.fixture
def staff(db):
    return User.objects.create_superuser(username="root", password="x", email="r@e.com")


def test_the_changelist_shows_progress(client, staff, waiting_run):
    batch = a_batch(waiting_run, total=4)
    StatusBatch.objects.filter(pk=batch.pk).update(finished=3, succeeded=3)
    client.force_login(staff)

    response = client.get(reverse("admin:state_machines_statusbatch_changelist"))

    assert response.status_code == 200
    assert b"3 / 4" in response.content


def test_the_changelist_can_be_ordered_by_least_done(client, staff, waiting_run):
    """Which is the ordering anybody debugging actually wants."""
    client.force_login(staff)
    a_batch(waiting_run, total=10)

    url = reverse("admin:state_machines_statusbatch_changelist")
    response = client.get(url, {"o": "4"})

    assert response.status_code == 200


def test_the_change_form_draws_the_tree(client, staff, waiting_run, row_version):
    parent = a_batch(waiting_run, total=1)
    row = ImportRow.objects.create(run=waiting_run, batch=parent)
    open_batch(row, join_action="import_row.process", parent_batch=parent)
    client.force_login(staff)

    response = client.get(reverse("admin:state_machines_statusbatch_change", args=[parent.pk]))

    assert response.status_code == 200
    assert b"nightly" in response.content


def test_a_failed_batch_reads_as_failed_in_the_bar(client, staff, waiting_run):
    batch = a_batch(waiting_run, total=4)
    StatusBatch.objects.filter(pk=batch.pk).update(finished=4, succeeded=1)
    client.force_login(staff)

    response = client.get(reverse("admin:state_machines_statusbatch_changelist"))

    assert b"3 failed" in response.content
