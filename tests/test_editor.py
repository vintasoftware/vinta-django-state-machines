"""The canvas document: serializing a version, and reconciling one back."""

from __future__ import annotations

import copy

import pytest

from vinta_state_machines.editor import (
    EditorPayloadError,
    action_catalog,
    apply_editor_machine,
    check_editor_machine,
    check_guard,
    editor_machine_template,
    empty_editor_machine,
    side_effect_definitions,
    to_editor_machine,
)
from vinta_state_machines.enums import HookEvent, HookTiming
from vinta_state_machines.models import (
    ActionType,
    StateMachineHook,
    StateMachineState,
    StateMachineTransition,
    StatusDefinition,
)
from vinta_state_machines.services import publish_version


@pytest.fixture
def machine(risk_draft):
    return to_editor_machine(risk_draft)


def state_of(document, key):
    return next(state for state in document["states"] if state["id"] == key)


def edge_of(document, name):
    return next(edge for edge in document["transitions"] if edge["name"] == name)


# ------------------------------------------------------------------ serializing


def test_states_carry_their_key_name_position_and_colour(machine):
    draft = state_of(machine, "draft")
    assert draft["name"] == "Draft"
    assert draft["position"] == {"x": 0, "y": 0}
    assert draft["color"] == "neutral"
    assert draft["onEnter"] == {"before": [], "after": []}


def test_initial_and_terminal_states_become_id_lists(machine):
    assert machine["initialStateIds"] == ["draft"]
    assert sorted(machine["finalStateIds"]) == ["mitigated", "rejected"]


def test_a_creation_edge_serializes_with_a_null_source(machine):
    assert edge_of(machine, "create")["from"] is None


def test_an_ordinary_edge_carries_its_trigger_guard_and_permission(machine):
    mitigate = edge_of(machine, "mitigate")
    assert mitigate["from"] == "assessed"
    assert mitigate["to"] == "mitigated"
    assert mitigate["trigger"] == {"id": "risk.mitigate", "name": "risk.mitigate"}
    assert mitigate["guard"] == "obj.amount <= 1000"
    assert edge_of(machine, "reject")["requiredPermission"] == "testapp.change_risk"


def test_what_the_editor_does_not_model_travels_in_the_data_blob(machine):
    assert edge_of(machine, "discard")["data"] == {"requires_approval": True}
    assert machine["data"]["machine"] == "risk.status"


def test_hooks_become_the_ordered_lists_they_belong_to(risk_draft):
    state = risk_draft.states.get(status__key="assessed")
    edge = risk_draft.transitions.get(name="mitigate")
    StateMachineHook.objects.create(
        state_machine_version=risk_draft,
        handler_key="testapp.record",
        timing=HookTiming.AFTER,
        event=HookEvent.ENTER_STATE,
        state=state,
        params={"tag": "in"},
        order=0,
    )
    StateMachineHook.objects.create(
        state_machine_version=risk_draft,
        handler_key="testapp.veto",
        timing=HookTiming.BEFORE,
        event=HookEvent.TRANSITION,
        transition=edge,
        on_commit=False,
        order=0,
    )

    document = to_editor_machine(risk_draft)
    entered = state_of(document, "assessed")["onEnter"]["after"]
    assert [effect["definitionId"] for effect in entered] == ["testapp.record"]
    assert entered[0]["params"] == {"tag": "in"}
    assert entered[0]["enabled"] is True
    assert entered[0]["data"] == {"on_commit": False}

    before = edge_of(document, "mitigate")["effects"]["before"]
    assert [effect["definitionId"] for effect in before] == ["testapp.veto"]


def test_the_catalogs_describe_handlers_and_actions(risk_draft):
    keys = {definition["id"] for definition in side_effect_definitions()}
    assert "testapp.record" in keys
    assert {action["id"] for action in action_catalog()} >= {"risk.assess", "risk.create"}


# -------------------------------------------------------------------- applying


def test_a_round_trip_changes_nothing(risk_draft, machine):
    before = list(risk_draft.states.values_list("status__key", "order", "x", "y"))
    edges = list(risk_draft.transitions.values_list("name", "action_type__key", "from_state"))
    apply_editor_machine(risk_draft, machine)

    assert list(risk_draft.states.values_list("status__key", "order", "x", "y")) == before
    assert (
        list(risk_draft.transitions.values_list("name", "action_type__key", "from_state")) == edges
    )
    assert to_editor_machine(risk_draft) == machine


def test_edge_order_is_numbered_within_each_source_state(risk_draft, machine):
    """``order`` only ever settles a race between edges leaving the *same* state.

    ``define_machine`` numbers them across the whole version, which sorts the same way
    but leaves gaps; reconciling normalizes each source's edges to 0, 1, 2 ...
    """
    apply_editor_machine(risk_draft, machine)
    by_source: dict[str | None, list[int]] = {}
    for edge in risk_draft.transitions.select_related("from_state__status"):
        key = edge.from_state.status.key if edge.from_state else None
        by_source.setdefault(key, []).append(edge.order)
    assert all(sorted(orders) == list(range(len(orders))) for orders in by_source.values())


def test_primary_keys_survive_an_edit(risk_draft, machine):
    edge = risk_draft.transitions.get(name="assess")
    document = copy.deepcopy(machine)
    edge_of(document, "assess")["description"] = "Send it to the assessor."
    apply_editor_machine(risk_draft, document)
    edge.refresh_from_db()
    assert edge.description == "Send it to the assessor."


def test_dragging_a_state_moves_it(risk_draft, machine):
    document = copy.deepcopy(machine)
    state_of(document, "draft")["position"] = {"x": 120.6, "y": -40.2}
    apply_editor_machine(risk_draft, document)
    state = risk_draft.states.get(status__key="draft")
    assert (state.x, state.y) == (121, -40)


def test_a_dragged_transition_card_keeps_its_offset(risk_draft, machine):
    document = copy.deepcopy(machine)
    edge_of(document, "assess")["labelOffset"] = {"x": 30, "y": -12}
    apply_editor_machine(risk_draft, document)
    edge = risk_draft.transitions.get(name="assess")
    assert (edge.label_offset_x, edge.label_offset_y) == (30, -12)
    assert to_editor_machine(risk_draft)["transitions"]


def test_a_state_drawn_on_the_canvas_gets_a_key_from_its_name(risk_draft, machine):
    document = copy.deepcopy(machine)
    document["states"].append(
        {
            "id": "state_9f2c",
            "name": "Under review",
            "position": {"x": 600, "y": 0},
            "color": "info",
            "description": "",
            "onEnter": {"before": [], "after": []},
            "onLeave": {"before": [], "after": []},
            "data": {},
        }
    )
    document["transitions"].append(
        {
            "id": "transition_77",
            "name": "review",
            "from": "assessed",
            "to": "state_9f2c",
            "trigger": {"id": "risk.review", "name": "Review"},
            "guard": "",
            "requiredPermission": "",
            "description": "",
            "labelOffset": {"x": 0, "y": 0},
            "effects": {"before": [], "after": []},
            "data": {},
        }
    )
    apply_editor_machine(risk_draft, document)

    state = risk_draft.states.get(status__key="under-review")
    assert state.color == "info"
    assert state.status.name == "Under review"
    edge = risk_draft.transitions.get(name="review")
    assert edge.to_state_id == state.pk
    assert edge.action_type.name == "Review"
    # The generated ids are gone once the server has spoken.
    assert state_of(to_editor_machine(risk_draft), "under-review")["name"] == "Under review"


def test_a_new_state_key_does_not_collide_with_an_existing_one(risk_draft, machine):
    document = copy.deepcopy(machine)
    document["states"].append(
        {
            "id": "state_dup",
            "name": "Draft",
            "position": {"x": 0, "y": 300},
            "color": "neutral",
            "description": "",
            "onEnter": {"before": [], "after": []},
            "onLeave": {"before": [], "after": []},
            "data": {},
        }
    )
    apply_editor_machine(risk_draft, document)
    assert risk_draft.states.filter(status__key="draft-2").exists()


def test_removing_a_state_removes_it_and_its_edges(risk_draft, machine):
    document = copy.deepcopy(machine)
    document["states"] = [state for state in document["states"] if state["id"] != "rejected"]
    document["transitions"] = [
        edge for edge in document["transitions"] if edge["to"] != "rejected"
    ]
    document["finalStateIds"] = ["mitigated"]
    apply_editor_machine(risk_draft, document)

    assert not risk_draft.states.filter(status__key="rejected").exists()
    assert not risk_draft.transitions.filter(name="reject").exists()
    # The vocabulary is shared and additive: dropping a state does not retire the word.
    assert StatusDefinition.objects.filter(entity_type="risk", key="rejected").exists()


def test_the_initial_and_final_lists_drive_the_flags(risk_draft, machine):
    document = copy.deepcopy(machine)
    document["initialStateIds"] = ["draft", "assessed"]
    document["finalStateIds"] = []
    apply_editor_machine(risk_draft, document)
    assert risk_draft.states.get(status__key="assessed").is_initial is True
    assert risk_draft.states.filter(is_terminal=True).count() == 0


def test_edges_are_ordered_within_the_state_they_leave(risk_draft, machine):
    document = copy.deepcopy(machine)
    # Put 'discard' before 'assess'; both leave 'draft'.
    document["transitions"].sort(key=lambda edge: edge["name"] != "discard")
    apply_editor_machine(risk_draft, document)
    leaving_draft = risk_draft.transitions.filter(from_state__status__key="draft")
    assert list(leaving_draft.order_by("order").values_list("name", "order")) == [
        ("discard", 0),
        ("assess", 1),
    ]


def test_side_effects_round_trip_through_their_lists(risk_draft, machine):
    document = copy.deepcopy(machine)
    state_of(document, "assessed")["onEnter"]["after"] = [
        {
            "id": "effect_a",
            "definitionId": "testapp.record",
            "name": "testapp.record",
            "params": {"tag": "one"},
            "enabled": True,
            "description": "first",
            "data": {"on_commit": True},
        },
        {
            "id": "effect_b",
            "definitionId": "testapp.bump_amount",
            "name": "testapp.bump_amount",
            "params": {},
            "enabled": False,
            "description": "",
            "data": {},
        },
    ]
    apply_editor_machine(risk_draft, document)

    hooks = risk_draft.hooks.filter(event=HookEvent.ENTER_STATE).order_by("order")
    assert [(hook.handler_key, hook.order) for hook in hooks] == [
        ("testapp.record", 0),
        ("testapp.bump_amount", 1),
    ]
    assert hooks[0].on_commit is True
    assert hooks[0].description == "first"
    assert hooks[1].is_active is False

    again = to_editor_machine(risk_draft)
    assert [e["definitionId"] for e in state_of(again, "assessed")["onEnter"]["after"]] == [
        "testapp.record",
        "testapp.bump_amount",
    ]


def test_reordering_a_side_effect_list_persists(risk_draft, machine):
    document = copy.deepcopy(machine)
    state_of(document, "assessed")["onLeave"]["before"] = [
        {
            "id": f"effect_{n}",
            "definitionId": key,
            "name": key,
            "params": {},
            "enabled": True,
            "description": "",
            "data": {},
        }
        for n, key in enumerate(["testapp.record", "testapp.veto"])
    ]
    apply_editor_machine(risk_draft, document)
    saved = to_editor_machine(risk_draft)
    listed = state_of(saved, "assessed")["onLeave"]["before"]

    swapped = copy.deepcopy(saved)
    state_of(swapped, "assessed")["onLeave"]["before"] = [listed[1], listed[0]]
    apply_editor_machine(risk_draft, swapped)

    hooks = risk_draft.hooks.filter(event=HookEvent.LEAVE_STATE).order_by("order")
    assert [hook.handler_key for hook in hooks] == ["testapp.veto", "testapp.record"]
    # Reordering must not have recreated the rows.
    assert {str(hook.pk) for hook in hooks} == {listed[0]["id"], listed[1]["id"]}


def test_an_any_transition_hook_is_invisible_and_survives(risk_draft, machine):
    hook = StateMachineHook.objects.create(
        state_machine_version=risk_draft,
        handler_key="testapp.record",
        timing=HookTiming.AFTER,
        event=HookEvent.ANY_TRANSITION,
    )
    document = to_editor_machine(risk_draft)
    assert all(state["onEnter"] == {"before": [], "after": []} for state in document["states"])
    apply_editor_machine(risk_draft, document)
    assert StateMachineHook.objects.filter(pk=hook.pk).exists()


# ---------------------------------------------------------------------- refusals


def test_a_published_version_is_refused(risk_draft, machine):
    publish_version(risk_draft)
    risk_draft.refresh_from_db()
    with pytest.raises(EditorPayloadError, match="can no longer be edited"):
        apply_editor_machine(risk_draft, machine)


def test_an_edge_without_a_trigger_is_refused(risk_draft, machine):
    document = copy.deepcopy(machine)
    edge_of(document, "assess")["trigger"] = None
    with pytest.raises(EditorPayloadError, match="has no trigger"):
        apply_editor_machine(risk_draft, document)


def test_an_unusable_guard_is_refused(risk_draft, machine):
    document = copy.deepcopy(machine)
    edge_of(document, "assess")["guard"] = "obj.amount ==="
    with pytest.raises(EditorPayloadError, match="unusable"):
        apply_editor_machine(risk_draft, document)


def test_a_document_from_another_machine_is_refused(risk_draft, machine):
    document = copy.deepcopy(machine)
    document["data"]["machine"] = "invoice.status"
    with pytest.raises(EditorPayloadError, match="belongs to"):
        apply_editor_machine(risk_draft, document)


def test_a_refusal_rolls_the_whole_document_back(risk_draft, machine):
    document = copy.deepcopy(machine)
    state_of(document, "draft")["position"] = {"x": 999, "y": 999}
    edge_of(document, "assess")["trigger"] = None
    with pytest.raises(EditorPayloadError):
        apply_editor_machine(risk_draft, document)
    assert risk_draft.states.get(status__key="draft").x == 0


def test_an_unknown_colour_is_refused(risk_draft, machine):
    document = copy.deepcopy(machine)
    state_of(document, "draft")["color"] = "chartreuse"
    with pytest.raises(EditorPayloadError, match="unknown colour"):
        apply_editor_machine(risk_draft, document)


def test_check_guard_answers_the_editors_validator(risk_draft):
    assert check_guard("") == {"ok": True}
    assert check_guard("obj.amount <= 10") == {"ok": True}
    verdict = check_guard("obj.amount <=")
    assert verdict["ok"] is False and verdict["errors"]


def test_applying_leaves_no_orphan_rows(risk_draft, machine):
    apply_editor_machine(risk_draft, machine)
    assert StateMachineState.objects.count() == len(machine["states"])
    assert StateMachineTransition.objects.count() == len(machine["transitions"])
    assert ActionType.objects.count() == 5


# ------------------------------------------------------- checking without saving


def refused_by_apply(version, document) -> list[str]:
    """Whatever ``apply_editor_machine`` would say about ``document``."""
    with pytest.raises(EditorPayloadError) as caught:
        apply_editor_machine(version, document)
    return caught.value.errors


def test_a_document_that_applies_cleanly_is_checked_clean(machine):
    assert check_editor_machine(machine) == []
    assert check_editor_machine(empty_editor_machine()) == []


def test_the_check_and_the_reconciliation_agree_on_every_refusal(risk_draft, machine):
    broken = {
        "no trigger": lambda doc: edge_of(doc, "assess").__setitem__("trigger", None),
        "unusable guard": lambda doc: edge_of(doc, "assess").__setitem__(
            "guard", "obj.amount ==="
        ),
        "unknown colour": lambda doc: state_of(doc, "draft").__setitem__("color", "chartreuse"),
        "nameless edge": lambda doc: edge_of(doc, "assess").__setitem__("name", "  "),
        "dangling edge": lambda doc: edge_of(doc, "assess").__setitem__("to", "nowhere"),
        "handlerless effect": lambda doc: state_of(doc, "draft")["onEnter"]["before"].append(
            {"id": "effect_1", "name": "?", "params": {}, "enabled": True, "data": {}}
        ),
    }
    for label, break_it in broken.items():
        document = copy.deepcopy(machine)
        break_it(document)
        assert check_editor_machine(document) == refused_by_apply(risk_draft, document), label


def test_a_document_that_is_not_an_object_is_refused_by_both(risk_draft):
    assert check_editor_machine("nope") == ["The machine must be an object."]
    assert refused_by_apply(risk_draft, "nope") == ["The machine must be an object."]


def test_the_check_never_writes_anything(risk_draft, machine):
    document = copy.deepcopy(machine)
    document["states"].append(
        {
            "id": "state_new",
            "name": "Brand new",
            "position": {"x": 0, "y": 0},
            "onEnter": {"before": [], "after": []},
            "onLeave": {"before": [], "after": []},
            "data": {},
        }
    )
    before = StatusDefinition.objects.count()

    assert check_editor_machine(document) == []
    assert StatusDefinition.objects.count() == before


# ---------------------------------------------------------------- seed documents


def test_an_empty_document_stamps_the_machine_it_is_for(risk_machine):
    assert empty_editor_machine()["data"] == {}
    assert empty_editor_machine(risk_machine)["data"] == {"machine": "risk.status"}


def test_the_template_is_the_latest_version_with_its_row_ids_blanked(risk_machine):
    previous = risk_machine.versions.get()
    template = editor_machine_template(risk_machine)
    document = to_editor_machine(previous)

    assert template["states"] == document["states"]
    assert [edge["name"] for edge in template["transitions"]] == [
        edge["name"] for edge in document["transitions"]
    ]
    assert all(edge["id"].startswith("transition_") for edge in template["transitions"])
    assert template["data"] == {"machine": "risk.status"}


def test_the_template_of_a_machine_with_no_version_is_an_empty_canvas(db, risk_machine):
    from vinta_state_machines.models import StateMachine

    fresh = StateMachine.objects.create(
        key="order.status",
        entity_type="order",
        status_field="status",
        scope=risk_machine.scope,
        name="Order status",
    )
    assert editor_machine_template(fresh) == empty_editor_machine(fresh)


def test_the_template_applies_onto_a_new_version_as_a_copy(risk_machine):
    from vinta_state_machines.models import StateMachineVersion

    template = editor_machine_template(risk_machine)
    fresh = StateMachineVersion.objects.create(state_machine=risk_machine, version="2")
    apply_editor_machine(fresh, template)

    previous = risk_machine.versions.get(version="1")
    assert fresh.states.count() == previous.states.count()
    assert fresh.transitions.count() == previous.transitions.count()
    assert previous.transitions.exclude(state_machine_version=previous).count() == 0


def test_a_draft_nobody_drew_on_is_not_what_the_next_version_starts_from(risk_machine):
    """An empty version filed after the real one must not become the template."""
    from vinta_state_machines.models import StateMachineVersion

    StateMachineVersion.objects.create(state_machine=risk_machine, version="2")

    template = editor_machine_template(risk_machine)
    assert [state["id"] for state in template["states"]] == [
        "draft",
        "assessed",
        "mitigated",
        "rejected",
    ]
