"""The query helpers on the versioned models and the history log."""

from __future__ import annotations

import pytest

from tests.testapp.models import Risk, Roadmap
from vinta_state_machines.engine import transition
from vinta_state_machines.models import StateMachineVersion, StatusTransition
from vinta_state_machines.services import clone_version

pytestmark = pytest.mark.django_db


def test_versions_can_be_narrowed_by_lifecycle(risk_version):
    draft = clone_version(risk_version, "2")

    assert list(StateMachineVersion.objects.published()) == [risk_version]
    assert list(StateMachineVersion.objects.draft()) == [draft]
    assert list(StateMachineVersion.objects.archived()) == []


def test_versions_can_be_narrowed_by_machine(risk_version):
    assert list(StateMachineVersion.objects.for_machine("risk.status")) == [risk_version]
    assert list(StateMachineVersion.objects.for_machine("nope")) == []


def test_with_graph_prefetches_everything_build_graph_reads(
    risk_version, django_assert_num_queries
):
    version = StateMachineVersion.objects.with_graph().get(pk=risk_version.pk)
    with django_assert_num_queries(0):
        graph = version.graph()
    assert len(graph.states) == 4
    assert len(graph.transitions) == 5


def test_without_the_prefetch_the_graph_still_avoids_n_plus_one(
    risk_version, django_assert_num_queries
):
    version = StateMachineVersion.objects.select_related("state_machine").get(pk=risk_version.pk)
    with django_assert_num_queries(3):
        # One query each for the states, the transitions and the hooks.
        version.graph()


def test_history_for_one_object_is_scoped_to_that_object(risk_version):
    first = Risk.objects.create(title="First")
    second = Risk.objects.create(title="Second")
    transition(first, "risk.assess")

    assert StatusTransition.objects.for_object(first).count() == 1
    assert StatusTransition.objects.for_object(second).count() == 0


def test_history_can_be_narrowed_to_one_status_field(risk_version):
    risk = Risk.objects.create(title="Only status")
    transition(risk, "risk.assess")

    assert StatusTransition.objects.for_object(risk, status_field="status").count() == 1
    assert StatusTransition.objects.for_object(risk, status_field="other").count() == 0


def test_history_for_a_model_covers_every_row_of_it(risk_version):
    for title in ("A", "B"):
        transition(Risk.objects.create(title=title), "risk.assess")

    assert StatusTransition.objects.for_model(Risk).count() == 2
    assert StatusTransition.objects.for_model(Roadmap).count() == 0


def test_entering_and_leaving_filter_by_status_key(risk):
    transition(risk, "risk.assess")
    transition(risk, "risk.mitigate")

    assert StatusTransition.objects.entering("mitigated").count() == 1
    assert StatusTransition.objects.leaving("draft").count() == 1
    assert StatusTransition.objects.leaving("mitigated").count() == 0


def test_with_related_loads_the_catalog_rows_in_one_go(risk, django_assert_num_queries):
    transition(risk, "risk.assess")
    with django_assert_num_queries(1):
        row = StatusTransition.objects.with_related().get()
        assert row.from_status.key == "draft"
        assert row.to_status.key == "assessed"
        assert row.action_type.key == "risk.assess"
        assert row.state_machine_version.version == "1"
