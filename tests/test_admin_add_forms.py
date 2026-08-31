"""Adding a machine, or a version of one, with its graph on the same form."""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from vinta_state_machines.editor import editor_machine_template, to_editor_machine
from vinta_state_machines.enums import Lifecycle
from vinta_state_machines.models import StateMachine, StateMachineVersion

pytestmark = pytest.mark.django_db


@pytest.fixture
def as_staff(client, db):
    client.force_login(User.objects.create_superuser(username="root", password="x", email=""))
    return client


ADD_MACHINE = "admin:state_machines_statemachine_add"
ADD_VERSION = "admin:state_machines_statemachineversion_add"
TEMPLATE_URL = "admin:state_machines_statemachineversion_editor_template"


def graph(*, states, transitions=(), initial=(), final=(), data=None) -> str:
    """A canvas document as the hidden field carries it: a JSON string."""
    return json.dumps(
        {
            "states": list(states),
            "transitions": list(transitions),
            "initialStateIds": list(initial),
            "finalStateIds": list(final),
            "data": data if data is not None else {},
        }
    )


def state(key: str, name: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": key,
        "name": name,
        "position": {"x": 0, "y": 0},
        "color": "neutral",
        "description": "",
        "onEnter": {"before": [], "after": []},
        "onLeave": {"before": [], "after": []},
        "data": {},
        **extra,
    }


def edge(name: str, source: str | None, target: str, action: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"transition_{name}",
        "name": name,
        "from": source,
        "to": target,
        "trigger": {"id": action, "name": action},
        "guard": "",
        "requiredPermission": "",
        "description": "",
        "labelOffset": {"x": 0, "y": 0},
        "effects": {"before": [], "after": []},
        "data": {},
        **extra,
    }


# ------------------------------------------------------------------ the machine


def test_the_add_form_carries_a_canvas_and_the_first_version_label(as_staff):
    body = as_staff.get(reverse(ADD_MACHINE)).content.decode()

    assert "<state-machine-editor>" in body
    assert 'name="graph"' in body
    assert 'name="version"' in body
    assert '<script type="module" src="/static/vinta_state_machines/admin-editor.js">' in body


def test_an_existing_machine_gets_the_live_canvas_not_the_add_form_one(as_staff, risk_machine):
    """Adding draws into the form; changing talks to an endpoint and publishes."""
    url = reverse("admin:state_machines_statemachine_change", args=[risk_machine.pk])
    body = as_staff.get(url).content.decode()

    assert "<state-machine-editor>" in body
    assert "data-machine-url=" in body
    assert "Save and publish a new version" in body
    # The first version's fields belong to the add form alone: this machine has
    # versions of its own, and the graph is saved on its endpoint rather than here.
    assert 'name="graph"' not in body
    assert 'name="version"' not in body


def test_saving_creates_the_machine_its_first_version_and_the_graph(as_staff):
    response = as_staff.post(
        reverse(ADD_MACHINE),
        {
            "key": "order.status",
            "entity_type": "order",
            "status_field": "status",
            "scope": "",
            "name": "Order status",
            "description": "",
            "author": "",
            "version": "1",
            "notes": "First cut.",
            "graph": graph(
                states=[state("draft", "Draft"), state("paid", "Paid")],
                transitions=[edge("pay", "draft", "paid", "order.pay")],
                initial=["draft"],
                final=["paid"],
            ),
        },
    )

    assert response.status_code == 302
    machine = StateMachine.objects.get(key="order.status")
    version = machine.versions.get()
    assert version.version == "1"
    assert version.lifecycle == Lifecycle.DRAFT
    assert version.notes == "First cut."
    assert [state.status.key for state in version.states.all()] == ["draft", "paid"]
    assert version.states.get(status__key="draft").is_initial
    assert version.states.get(status__key="paid").is_terminal
    edge_row = version.transitions.get()
    assert (edge_row.name, edge_row.from_state.status.key, edge_row.to_state.status.key) == (
        "pay",
        "draft",
        "paid",
    )


def test_the_scope_free_machine_lands_in_the_global_scope(as_staff):
    """The blank scope select is the fallback machine, exactly as it is on a change."""
    as_staff.post(
        reverse(ADD_MACHINE),
        {
            "key": "order.status",
            "entity_type": "order",
            "status_field": "status",
            "scope": "",
            "name": "Order status",
            "description": "",
            "author": "",
            "version": "1",
            "notes": "",
            "graph": graph(states=[state("draft", "Draft")], initial=["draft"]),
        },
    )
    assert StateMachine.objects.filter(key="order.status").exists()


def test_a_machine_can_still_be_created_with_nothing_drawn_yet(as_staff):
    response = as_staff.post(
        reverse(ADD_MACHINE),
        {
            "key": "order.status",
            "entity_type": "order",
            "status_field": "status",
            "scope": "",
            "name": "Order status",
            "description": "",
            "author": "",
            "version": "1",
            "notes": "",
            "graph": "",
        },
    )

    assert response.status_code == 302
    version = StateMachine.objects.get(key="order.status").versions.get()
    assert not version.states.exists()


def test_a_refused_graph_comes_back_on_the_form_rather_than_being_saved(as_staff):
    response = as_staff.post(
        reverse(ADD_MACHINE),
        {
            "key": "order.status",
            "entity_type": "order",
            "status_field": "status",
            "scope": "",
            "name": "Order status",
            "description": "",
            "author": "",
            "version": "1",
            "notes": "",
            "graph": graph(
                states=[state("draft", "Draft"), state("paid", "Paid")],
                # A transition nobody picked an action for.
                transitions=[edge("pay", "draft", "paid", "order.pay") | {"trigger": None}],
            ),
        },
    )

    assert response.status_code == 200
    assert "has no trigger" in response.content.decode()
    assert not StateMachine.objects.filter(key="order.status").exists()


def test_the_author_defaults_to_whoever_filled_the_form_in(as_staff):
    as_staff.post(
        reverse(ADD_MACHINE),
        {
            "key": "order.status",
            "entity_type": "order",
            "status_field": "status",
            "scope": "",
            "name": "Order status",
            "description": "",
            "author": "",
            "version": "1",
            "notes": "",
            "graph": "",
        },
    )

    machine = StateMachine.objects.get(key="order.status")
    assert machine.author is not None
    assert machine.author.identity_label == "root"


# ------------------------------------------------------------------ the version


def test_the_version_add_form_carries_a_canvas_and_a_template_endpoint(as_staff):
    body = as_staff.get(reverse(ADD_VERSION)).content.decode()

    assert "<state-machine-editor>" in body
    assert 'name="graph"' in body
    assert reverse(TEMPLATE_URL) in body
    assert 'data-source-field="id_state_machine"' in body
    # A version being drawn is a draft: there is nothing to choose.
    assert 'name="lifecycle"' not in body


def test_the_template_endpoint_answers_with_the_latest_version(as_staff, risk_machine):
    response = as_staff.get(reverse(TEMPLATE_URL), {"state_machine": risk_machine.pk})

    assert response.status_code == 200
    assert response.json() == editor_machine_template(risk_machine)
    assert {entry["id"] for entry in response.json()["states"]} == {
        "draft",
        "assessed",
        "mitigated",
        "rejected",
    }


def test_the_template_endpoint_answers_with_an_empty_canvas_for_no_machine(as_staff):
    for query in (
        {},
        {"state_machine": ""},
        {"state_machine": "nonsense"},
        {"state_machine": 999},
    ):
        payload = as_staff.get(reverse(TEMPLATE_URL), query).json()
        assert payload["states"] == []
        assert payload["transitions"] == []


def test_the_template_carries_no_id_of_the_version_it_came_from(as_staff, risk_machine):
    previous = risk_machine.versions.get()
    template = editor_machine_template(risk_machine)

    row_ids = {str(edge.pk) for edge in previous.transitions.all()}
    assert row_ids
    assert not row_ids & {edge["id"] for edge in template["transitions"]}
    assert all(edge["id"].startswith("transition_") for edge in template["transitions"])
    assert template["data"] == {"machine": risk_machine.key}


def test_saving_a_new_version_applies_the_graph_it_was_drawn_with(as_staff, risk_machine):
    document = editor_machine_template(risk_machine)
    document["states"].append(state("archived", "Archived"))
    document["finalStateIds"].append("archived")
    document["transitions"].append(edge("archive", "rejected", "archived", "risk.archive"))

    response = as_staff.post(
        reverse(ADD_VERSION),
        {
            "state_machine": str(risk_machine.pk),
            "version": "2",
            "notes": "",
            "graph": json.dumps(document),
        },
    )

    assert response.status_code == 302
    version = risk_machine.versions.get(version="2")
    assert version.lifecycle == Lifecycle.DRAFT
    assert {state.status.key for state in version.states.all()} == {
        "draft",
        "assessed",
        "mitigated",
        "rejected",
        "archived",
    }
    assert version.transitions.filter(name="archive").exists()
    # The previous version is left exactly as it was.
    assert risk_machine.versions.get(version="1").transitions.count() == 5


def test_a_new_version_starts_from_the_previous_one_untouched(as_staff, risk_machine):
    previous = risk_machine.versions.get()

    as_staff.post(
        reverse(ADD_VERSION),
        {
            "state_machine": str(risk_machine.pk),
            "version": "2",
            "notes": "",
            "graph": json.dumps(editor_machine_template(risk_machine)),
        },
    )

    fresh = risk_machine.versions.get(version="2")
    before = to_editor_machine(previous)
    after = to_editor_machine(fresh)
    # Same graph, different rows: only the ids and the version stamp differ.
    assert after["states"] == before["states"]
    assert [edge["name"] for edge in after["transitions"]] == [
        edge["name"] for edge in before["transitions"]
    ]
    assert {edge["id"] for edge in after["transitions"]}.isdisjoint(
        {edge["id"] for edge in before["transitions"]}
    )


def test_a_graph_drawn_for_another_machine_is_refused(as_staff, risk_machine):
    other = StateMachine.objects.create(
        key="order.status",
        entity_type="order",
        status_field="status",
        scope=risk_machine.scope,
        name="Order status",
    )
    document = editor_machine_template(risk_machine)

    response = as_staff.post(
        reverse(ADD_VERSION),
        {
            "state_machine": str(other.pk),
            "version": "1",
            "notes": "",
            "graph": json.dumps(document),
        },
    )

    assert response.status_code == 200
    assert "belongs to" in response.content.decode()
    assert not StateMachineVersion.objects.filter(state_machine=other).exists()


def test_a_refused_version_graph_saves_nothing(as_staff, risk_machine):
    document = editor_machine_template(risk_machine)
    document["transitions"][0]["trigger"] = None

    response = as_staff.post(
        reverse(ADD_VERSION),
        {
            "state_machine": str(risk_machine.pk),
            "version": "2",
            "notes": "",
            "graph": json.dumps(document),
        },
    )

    assert response.status_code == 200
    assert "has no trigger" in response.content.decode()
    assert not risk_machine.versions.filter(version="2").exists()


def test_an_existing_version_keeps_its_live_canvas(as_staff, risk_draft):
    url = reverse("admin:state_machines_statemachineversion_change", args=[risk_draft.pk])
    body = as_staff.get(url).content.decode()

    assert "data-machine-url" in body
    assert "data-dsm-save" in body
    assert 'name="graph"' not in body
