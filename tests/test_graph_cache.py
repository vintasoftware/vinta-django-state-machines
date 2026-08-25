"""The graph cache, which ships on by default but is disabled in the test settings."""

from __future__ import annotations

import pytest

from vinta_state_machines.graph import build_graph, clear_graph_cache, get_graph
from vinta_state_machines.models import StateMachineVersion

pytestmark = pytest.mark.django_db


@pytest.fixture
def caching(settings):
    settings.STATE_MACHINES = {"CACHE_GRAPHS": True}
    clear_graph_cache()
    yield
    clear_graph_cache()


def test_a_second_read_of_the_same_version_costs_nothing(
    caching, risk_version, django_assert_num_queries
):
    get_graph(risk_version)
    with django_assert_num_queries(0):
        assert get_graph(risk_version).machine_key == "risk.status"


def test_editing_the_version_invalidates_the_cached_graph(caching, risk_version):
    assert len(get_graph(risk_version).states) == 4

    risk_version.states.filter(status__key="rejected").delete()
    risk_version.refresh_from_db()
    assert len(get_graph(risk_version).states) == 3


def test_a_touched_version_is_rebuilt_even_without_the_signal(caching, risk_version):
    """The cache also compares ``modified_at``, so it cannot serve a stale graph."""
    cached = get_graph(risk_version)
    risk_version.states.filter(status__key="rejected").delete()

    stale = StateMachineVersion.objects.select_related("state_machine").get(pk=risk_version.pk)
    fresh = get_graph(stale)
    assert fresh is not cached
    assert len(fresh.states) == 3


def test_a_version_can_be_read_by_primary_key(caching, risk_version):
    assert get_graph(risk_version.pk).version_label == "1"


def test_clearing_the_cache_forces_a_rebuild(caching, risk_version, django_assert_num_queries):
    get_graph(risk_version)
    clear_graph_cache()
    with django_assert_num_queries(3):
        get_graph(risk_version)


def test_with_caching_off_every_read_rebuilds(settings, risk_version, django_assert_num_queries):
    settings.STATE_MACHINES = {"CACHE_GRAPHS": False}
    get_graph(risk_version)
    with django_assert_num_queries(3):
        get_graph(risk_version)


def test_build_graph_skips_a_transition_pointing_outside_its_version(risk_version):
    """Validation reports such a row; the builder must not blow up on it meanwhile."""
    from vinta_state_machines.services import clone_version

    other = clone_version(risk_version, "2")
    edge = risk_version.transitions.filter(from_state__isnull=False).first()
    edge.to_state = other.states.first()
    edge.save(update_fields=["to_state"])

    graph = build_graph(risk_version)
    assert edge.pk not in {spec.pk for spec in graph.transitions}
