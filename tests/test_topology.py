"""State positions, named edges, self transitions and parallel edges."""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from tests.testapp.models import Risk
from vinta_state_machines.engine import (
    available_transitions,
    can_transition,
    transition,
)
from vinta_state_machines.exceptions import GuardFailed, PermissionDenied, TransitionNotAllowed
from vinta_state_machines.models import (
    StateMachineState,
    StateMachineTransition,
    StatusTransition,
)
from vinta_state_machines.services import define_machine, publish_version, validate_version

pytestmark = pytest.mark.django_db


def edge(version, name, source, target, action, **kwargs):
    """Add one edge to a version, resolving the states by status key."""
    return StateMachineTransition.objects.create(
        state_machine_version=version,
        name=name,
        from_state=version.states.get(status__key=source) if source else None,
        to_state=version.states.get(status__key=target),
        action_type=version.transitions.get(name="assess").action_type
        if action == "risk.assess"
        else _action(action),
        **kwargs,
    )


def _action(key):
    from vinta_state_machines.models import ActionType

    return ActionType.objects.get_or_create(key=key, defaults={"name": key})[0]


# ------------------------------------------------------------------- positions


def test_states_carry_a_position_on_the_canvas(risk_version):
    graph = risk_version.graph()
    assert graph.state("draft").position == (0, 0)
    assert graph.state("mitigated").position == (400, -80)


def test_positions_default_to_the_origin(risk_version):
    from vinta_state_machines.models import StatusDefinition

    status = StatusDefinition.objects.create(
        entity_type="risk", status_field="status", key="parked", name="Parked"
    )
    state = StateMachineState.objects.create(state_machine_version=risk_version, status=status)
    assert (state.x, state.y) == (0, 0)


def test_positions_may_be_negative(risk_version):
    risk_version.states.filter(status__key="draft").update(x=-120, y=-40)
    assert risk_version.graph().state("draft").position == (-120, -40)


def test_positions_survive_a_clone(risk_version):
    from vinta_state_machines.services import clone_version

    clone = clone_version(risk_version, "2")
    assert clone.graph().state("mitigated").position == (400, -80)


def test_positions_round_trip_through_export_and_import(risk_version, tmp_path):
    import json
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    call_command("export_state_machine", "risk.status", stdout=out)
    exported = json.loads(out.getvalue())
    by_key = {state["key"]: state for state in exported["states"]}
    assert (by_key["mitigated"]["x"], by_key["mitigated"]["y"]) == (400, -80)


# ----------------------------------------------------------------------- names


def test_every_edge_is_named(risk_version):
    assert {spec.name for spec in risk_version.graph().transitions} == {
        "create",
        "assess",
        "mitigate",
        "reject",
        "discard",
    }


def test_an_edge_can_be_looked_up_by_name(risk_version):
    graph = risk_version.graph()
    assert graph.named("draft", "assess").action == "risk.assess"
    assert graph.named("draft", "mitigate") is None


def test_names_are_unique_among_the_edges_leaving_one_state(risk_version):
    with pytest.raises(IntegrityError), transaction.atomic():
        edge(risk_version, "assess", "draft", "mitigated", "risk.assess")


def test_the_same_name_may_leave_two_different_states(risk_version):
    edge(risk_version, "assess", "assessed", "assessed", "risk.reassess")
    assert len(risk_version.graph().transitions) == 6


def test_creation_edges_also_have_unique_names(risk_version):
    with pytest.raises(IntegrityError), transaction.atomic():
        edge(risk_version, "create", None, "draft", "risk.recreate")


def test_a_named_edge_can_be_targeted_explicitly(risk):
    transition(risk, "risk.assess", transition_name="assess")
    assert risk.status_key == "assessed"


def test_targeting_an_edge_that_does_not_leave_this_state_is_refused(risk):
    with pytest.raises(TransitionNotAllowed, match="no transition named 'mitigate'"):
        transition(risk, "risk.mitigate", transition_name="mitigate")


def test_targeting_an_edge_with_the_wrong_action_is_refused(risk):
    with pytest.raises(TransitionNotAllowed, match="is driven by 'risk.assess'"):
        transition(risk, "risk.discard", transition_name="assess")


def test_available_transitions_expose_their_names(risk, user):
    assert [option.name for option in available_transitions(risk, actor=user)] == ["assess"]


# ------------------------------------------------------------ self transitions


@pytest.fixture
def with_self_edge(risk_version):
    """``assessed --risk.reassess--> assessed``: a legal loop back onto the same state."""
    edge(risk_version, "reassess", "assessed", "assessed", "risk.reassess")
    return risk_version


def test_a_state_may_transition_to_itself(with_self_edge, risk):
    transition(risk, "risk.assess")
    record = transition(risk, "risk.reassess")

    assert risk.status_key == "assessed"
    assert (record.from_status.key, record.to_status.key) == ("assessed", "assessed")


def test_a_self_transition_is_reported_as_one(with_self_edge):
    graph = with_self_edge.graph()
    assert graph.named("assessed", "reassess").is_self_transition is True
    assert graph.named("draft", "assess").is_self_transition is False


def test_a_self_transition_is_offered_like_any_other(with_self_edge, risk, user):
    transition(risk, "risk.assess")
    assert "risk.reassess" in [option.action for option in available_transitions(risk, actor=user)]


def test_a_self_transition_is_no_longer_a_validation_warning(with_self_edge):
    report = validate_version(with_self_edge)
    assert report.ok
    assert not any("self loop" in warning for warning in report.warnings)


def test_a_terminal_state_still_may_not_loop_back_to_itself(risk_version):
    edge(risk_version, "linger", "mitigated", "mitigated", "risk.linger")
    report = validate_version(risk_version)
    assert any("is terminal but has the outgoing transition" in error for error in report.errors)


def test_a_self_transition_still_appends_history(with_self_edge, risk):
    transition(risk, "risk.assess")
    transition(risk, "risk.reassess")
    transition(risk, "risk.reassess")
    assert StatusTransition.objects.for_object(risk).count() == 3


# ---------------------------------------------------------------- parallel edges


@pytest.fixture
def parallel(risk_version):
    """Two edges ``assessed -> mitigated``, both under ``risk.mitigate``.

    The cheap one is unguarded and ordered last; the expensive one needs a permission
    and comes first, so resolution order is observable.
    """
    risk_version.transitions.filter(name="mitigate").update(order=10, guard="")
    edge(
        risk_version,
        "mitigate_large",
        "assessed",
        "mitigated",
        "risk.mitigate",
        guard="obj.amount > 1000",
        required_permission="testapp.change_risk",
        order=0,
    )
    return risk_version


def test_two_states_may_be_joined_by_several_edges(parallel):
    graph = parallel.graph()
    assert len(graph.candidates("assessed", "risk.mitigate")) == 2
    assert [spec.name for spec in graph.candidates("assessed", "risk.mitigate")] == [
        "mitigate_large",
        "mitigate",
    ]


def test_the_first_candidate_whose_guard_holds_wins(parallel, privileged_user):
    big = Risk.objects.create(title="Big", amount=5000)
    transition(big, "risk.assess")
    record = transition(big, "risk.mitigate", actor=privileged_user)
    assert big.status_key == "mitigated"
    assert record.transition.name == "mitigate_large"


def test_a_candidate_whose_guard_fails_is_skipped_for_the_next(parallel, user):
    small = Risk.objects.create(title="Small", amount=10)
    transition(small, "risk.assess")
    record = transition(small, "risk.mitigate", actor=user)
    assert record.transition.name == "mitigate"  # the guarded edge does not apply


def test_a_candidate_the_caller_may_not_take_is_skipped(parallel, user):
    """The permissioned edge is ordered first, but this user falls through to the other."""
    big = Risk.objects.create(title="Big", amount=5000)
    transition(big, "risk.assess")
    record = transition(big, "risk.mitigate", actor=user)
    assert record.transition.name == "mitigate"


def test_when_no_candidate_is_viable_the_first_one_explains_why(parallel, user):
    risk_version = parallel
    risk_version.transitions.filter(name="mitigate").update(guard="obj.amount < 0")
    big = Risk.objects.create(title="Big", amount=5000)
    transition(big, "risk.assess")
    with pytest.raises(PermissionDenied, match="testapp.change_risk"):
        transition(big, "risk.mitigate", actor=user)


def test_a_single_candidate_still_raises_its_own_specific_error(risk_version):
    big = Risk.objects.create(title="Big", amount=5000)
    transition(big, "risk.assess")
    with pytest.raises(GuardFailed, match="obj.amount <= 1000"):
        transition(big, "risk.mitigate")


def test_can_transition_is_true_when_any_candidate_is_viable(parallel, user):
    small = Risk.objects.create(title="Small", amount=10)
    transition(small, "risk.assess")
    assert can_transition(small, "risk.mitigate", actor=user) is True


def test_can_transition_narrowed_to_one_edge_answers_for_that_edge(parallel, user):
    small = Risk.objects.create(title="Small", amount=10)
    transition(small, "risk.assess")
    assert can_transition(small, "risk.mitigate", actor=user, transition_name="mitigate") is True
    assert (
        can_transition(small, "risk.mitigate", actor=user, transition_name="mitigate_large")
        is False
    )


def test_naming_an_edge_overrides_the_resolution_order(parallel, privileged_user):
    big = Risk.objects.create(title="Big", amount=5000)
    transition(big, "risk.assess")
    record = transition(big, "risk.mitigate", actor=privileged_user, transition_name="mitigate")
    assert record.transition.name == "mitigate"


def test_both_edges_are_listed_as_available(parallel, privileged_user):
    big = Risk.objects.create(title="Big", amount=5000)
    transition(big, "risk.assess")
    options = available_transitions(big, actor=privileged_user, include_blocked=True)
    assert {option.name for option in options} == {"mitigate", "mitigate_large", "reject"}


def test_parallel_edges_survive_publication(parallel):
    from vinta_state_machines.services import clone_version

    clone = clone_version(parallel, "2")
    publish_version(clone)
    assert len(clone.graph().candidates("assessed", "risk.mitigate")) == 2


# ------------------------------------------------------- declarative definitions


def test_a_definition_can_declare_a_self_transition_and_parallel_edges():
    version = define_machine(
        {
            "key": "ticket.status",
            "entity_type": "ticket",
            "status_field": "status",
            "name": "Ticket status",
            "version": "1",
            "states": [
                {"key": "open", "name": "Open", "is_initial": True, "x": 10, "y": 20},
                {"key": "closed", "name": "Closed", "is_terminal": True, "x": 300, "y": 20},
            ],
            "transitions": [
                {"name": "comment", "from": "open", "to": "open", "action": "ticket.comment"},
                {"name": "close", "from": "open", "to": "closed", "action": "ticket.close"},
                {
                    "name": "close_as_duplicate",
                    "from": "open",
                    "to": "closed",
                    "action": "ticket.close",
                    "guard": "metadata['duplicate']",
                    "order": 0,
                },
            ],
        }
    )
    graph = version.graph()

    assert graph.state("open").position == (10, 20)
    assert graph.named("open", "comment").is_self_transition
    assert [spec.name for spec in graph.candidates("open", "ticket.close")] == [
        "close_as_duplicate",
        "close",
    ]
    assert validate_version(version).ok


def test_an_unnamed_definition_edge_falls_back_to_its_action():
    version = define_machine(
        {
            "key": "note.status",
            "entity_type": "note",
            "status_field": "status",
            "name": "Note status",
            "version": "1",
            "states": [{"key": "new", "name": "New", "is_initial": True}],
            "transitions": [{"from": None, "to": "new", "action": "note.create"}],
        }
    )
    assert version.transitions.get().name == "note.create"
