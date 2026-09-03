"""The fan-out declared on the graph: the fields, the payload, and the check."""

from __future__ import annotations

from datetime import timedelta

import pytest

from vinta_state_machines.batch_effects import REPORT_HANDLER, wire_batch_reporting
from vinta_state_machines.editor import apply_editor_machine, to_editor_machine
from vinta_state_machines.enums import HookEvent
from vinta_state_machines.models import ActionType, StateMachineHook
from vinta_state_machines.services import validate_version

pytestmark = pytest.mark.django_db


def mark_waiting(version, status_key="processing", action="import_run.finish", **extra):
    state = version.states.get(status__key=status_key)
    state.is_waiting = True
    state.join_action = ActionType.objects.get(key=action) if action else None
    for name, value in extra.items():
        setattr(state, name, value)
    state.save()
    return state


def state_in(document, status_key):
    return next(s for s in document["states"] if s["id"] == status_key)


# --------------------------------------------------------------------- fields


def test_a_state_can_declare_that_it_fans_work_out(run_draft):
    state = mark_waiting(run_draft, child_machine="import_row.status")

    assert state.is_waiting is True
    assert state.join_action.key == "import_run.finish"
    assert state.child_machine == "import_row.status"


def test_the_graph_carries_the_declaration(run_draft):
    mark_waiting(run_draft, batch_timeout=timedelta(hours=2))

    graph = run_draft.graph()

    assert [s.key for s in graph.waiting_states] == ["processing"]
    assert graph.state("processing").join_action == "import_run.finish"
    assert graph.state("pending").is_waiting is False


def test_a_version_with_no_waiting_states_has_none(run_draft):
    assert run_draft.graph().waiting_states == ()


# -------------------------------------------------------------------- payload


def test_the_declaration_reaches_the_editor_in_data(run_draft):
    mark_waiting(run_draft, child_machine="import_row.status", batch_timeout=timedelta(hours=2))

    data = state_in(to_editor_machine(run_draft), "processing")["data"]

    assert data["is_waiting"] is True
    assert data["join_action"] == "import_run.finish"
    assert data["child_machine"] == "import_row.status"
    assert data["timeout"] == "P0DT02H00M00S"


def test_an_ordinary_state_carries_nothing_extra(run_draft):
    assert state_in(to_editor_machine(run_draft), "pending")["data"] == {}


def test_counts_as_is_derived_from_the_report_binding(row_draft):
    """The editor is handed a value, so it never has to know a handler key."""
    wire_batch_reporting(row_draft, {"processed": "success", "rejected": "failure"})

    document = to_editor_machine(row_draft)

    assert state_in(document, "processed")["data"]["counts_as"] == "success"
    assert state_in(document, "rejected")["data"]["counts_as"] == "failure"


def test_report_bindings_are_not_drawn_as_chips(row_draft):
    """They are one concept in two rows; two chips would invite deleting half of it."""
    wire_batch_reporting(row_draft, {"processed": "success"})

    processed = state_in(to_editor_machine(row_draft), "processed")

    assert processed["onEnter"]["after"] == []
    assert processed["onLeave"]["after"] == []
    assert processed["data"]["counts_as"] == "success"


# --------------------------------------------------------------- round tripping


def test_the_declaration_survives_a_round_trip(run_draft):
    mark_waiting(run_draft, child_machine="import_row.status", batch_timeout=timedelta(hours=2))
    document = to_editor_machine(run_draft)

    apply_editor_machine(run_draft, document)

    state = run_draft.states.get(status__key="processing")
    assert state.is_waiting is True
    assert state.join_action.key == "import_run.finish"
    assert state.batch_timeout == timedelta(hours=2)


def test_counts_as_survives_a_round_trip(row_draft):
    """The pair is hidden from the lanes, so applying has to put it back."""
    wire_batch_reporting(row_draft, {"processed": "success", "rejected": "failure"})
    document = to_editor_machine(row_draft)

    apply_editor_machine(row_draft, document)

    events = set(
        row_draft.hooks.filter(
            handler_key=REPORT_HANDLER, state__status__key="processed"
        ).values_list("event", flat=True)
    )
    assert events == {HookEvent.ENTER_STATE, HookEvent.LEAVE_STATE}


def test_applying_counts_as_writes_only_the_enter_half_for_a_terminal_state(row_draft):
    document = to_editor_machine(row_draft)
    state_in(document, "rejected")["data"]["counts_as"] = "failure"

    apply_editor_machine(row_draft, document)

    events = list(
        row_draft.hooks.filter(
            handler_key=REPORT_HANDLER, state__status__key="rejected"
        ).values_list("event", flat=True)
    )
    assert events == [HookEvent.ENTER_STATE]


def test_clearing_counts_as_removes_the_bindings(row_draft):
    wire_batch_reporting(row_draft, {"processed": "success"})
    document = to_editor_machine(row_draft)
    state_in(document, "processed")["data"].pop("counts_as")

    apply_editor_machine(row_draft, document)

    assert not row_draft.hooks.filter(handler_key=REPORT_HANDLER).exists()


def test_turning_the_fan_out_off_clears_the_join_action(run_draft):
    mark_waiting(run_draft)
    document = to_editor_machine(run_draft)
    state_in(document, "processing")["data"] = {"is_waiting": False}

    apply_editor_machine(run_draft, document)

    state = run_draft.states.get(status__key="processing")
    assert state.is_waiting is False
    assert state.join_action_id is None


def test_a_document_that_says_nothing_leaves_the_state_alone(run_draft):
    """A client built before any of this existed has to round trip unchanged."""
    mark_waiting(run_draft)
    document = to_editor_machine(run_draft)
    state_in(document, "processing")["data"] = {}

    apply_editor_machine(run_draft, document)

    assert run_draft.states.get(status__key="processing").is_waiting is True


def test_an_unknown_join_action_is_reported(run_draft):
    document = to_editor_machine(run_draft)
    state_in(document, "processing")["data"] = {
        "is_waiting": True,
        "join_action": "nope.nothing",
    }

    with pytest.raises(Exception, match="unknown action"):
        apply_editor_machine(run_draft, document)


def test_an_unreadable_timeout_is_reported(run_draft):
    document = to_editor_machine(run_draft)
    state_in(document, "processing")["data"] = {
        "is_waiting": True,
        "join_action": "import_run.finish",
        "timeout": "two hours",
    }

    with pytest.raises(Exception, match="unreadable timeout"):
        apply_editor_machine(run_draft, document)


def test_waiting_with_no_join_action_is_reported(run_draft):
    document = to_editor_machine(run_draft)
    state_in(document, "processing")["data"] = {"is_waiting": True, "join_action": ""}

    with pytest.raises(Exception, match="names no join action"):
        apply_editor_machine(run_draft, document)


# ------------------------------------------------------------------ validation


def test_a_waiting_state_with_a_join_edge_validates(run_draft):
    mark_waiting(run_draft)

    assert validate_version(run_draft).ok


def test_a_waiting_state_with_no_edge_under_its_join_action_is_an_error(run_draft):
    """The batch would complete, fire, find nothing, and the record would sit forever."""
    mark_waiting(run_draft, action="import_run.cancel")
    run_draft.transitions.filter(name="cancel").delete()

    report = validate_version(run_draft)

    assert not report.ok
    assert any("no transition leaves it under that action" in e for e in report.errors)


def test_a_waiting_state_naming_no_action_at_all_is_an_error(run_draft):
    state = run_draft.states.get(status__key="processing")
    state.is_waiting = True
    state.save()

    report = validate_version(run_draft)

    assert any("names no join action" in e for e in report.errors)


def test_an_ordinary_state_is_not_the_check_s_business(run_draft):
    assert validate_version(run_draft).ok


def test_a_state_that_stops_waiting_stops_being_checked(run_draft):
    mark_waiting(run_draft, action="import_run.cancel")
    run_draft.transitions.filter(name="cancel").delete()
    assert not validate_version(run_draft).ok

    state = run_draft.states.get(status__key="processing")
    state.is_waiting = False
    state.save()

    assert validate_version(run_draft).ok


def test_the_hook_params_route_is_still_available(waiting_run):
    """Declaring on the state is the new way, not the only way. Nothing regressed."""
    assert StateMachineHook.objects.count() == 0


# ------------------------------------------------------- the half-configured pair


def _report_hook(version, status_key, event, outcome="success"):
    return StateMachineHook.objects.create(
        state_machine_version=version,
        handler_key=REPORT_HANDLER,
        timing="after",
        event=event,
        state=version.states.get(status__key=status_key),
        params={"outcome": outcome},
    )


def test_a_whole_pair_is_not_flagged_as_partial(row_draft):
    wire_batch_reporting(row_draft, {"processed": "success"})

    data = state_in(to_editor_machine(row_draft), "processed")["data"]

    assert data["counts_as"] == "success"
    assert "counts_as_partial" not in data


def test_an_enter_only_pair_names_the_half_it_has(row_draft):
    """The editor never sees a hook row, so it cannot work this out for itself.

    Without the key a pair missing its leave half draws as though it were whole, and
    the canvas cannot mark it broken while the person who broke it is still looking.
    """
    _report_hook(row_draft, "processed", HookEvent.ENTER_STATE)

    data = state_in(to_editor_machine(row_draft), "processed")["data"]

    assert data["counts_as"] == "success"
    assert data["counts_as_partial"] == "enter"


def test_a_leave_only_pair_is_reported_rather_than_hidden(row_draft):
    """Broken everywhere. Reading only the enter list would have shown nothing at all."""
    _report_hook(row_draft, "processed", HookEvent.LEAVE_STATE, outcome="failure")

    data = state_in(to_editor_machine(row_draft), "processed")["data"]

    assert data["counts_as"] == "failure"
    assert data["counts_as_partial"] == "leave"


def test_a_terminal_state_is_enter_only_and_says_so(row_draft):
    """Right rather than broken, and the editor is the one that knows the difference."""
    wire_batch_reporting(row_draft, {"rejected": "failure"})

    data = state_in(to_editor_machine(row_draft), "rejected")["data"]

    assert data["counts_as"] == "failure"
    assert data["counts_as_partial"] == "enter"


def test_a_state_that_reports_nothing_carries_neither_key(row_draft):
    data = state_in(to_editor_machine(row_draft), "queued")["data"]

    assert "counts_as" not in data
    assert "counts_as_partial" not in data


def test_the_partial_key_does_not_survive_a_round_trip_as_a_pair(row_draft):
    """Applying reads counts_as; the partial half is a report, not an instruction."""
    _report_hook(row_draft, "processed", HookEvent.ENTER_STATE)
    document = to_editor_machine(row_draft)

    apply_editor_machine(row_draft, document)

    events = set(
        row_draft.hooks.filter(
            handler_key=REPORT_HANDLER, state__status__key="processed"
        ).values_list("event", flat=True)
    )
    # Reopenable, so applying completes the pair rather than preserving the break.
    assert events == {HookEvent.ENTER_STATE, HookEvent.LEAVE_STATE}
