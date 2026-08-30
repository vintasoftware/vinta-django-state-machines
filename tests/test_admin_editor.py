"""The canvas endpoints hanging off a version's change form."""

from __future__ import annotations

import copy
import json

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from vinta_state_machines.editor import to_editor_machine
from vinta_state_machines.services import publish_version

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(db) -> User:
    return User.objects.create_superuser(username="root", password="x", email="")


@pytest.fixture
def as_staff(client, staff):
    client.force_login(staff)
    return client


def machine_url(version) -> str:
    return reverse("admin:state_machines_statemachineversion_editor_machine", args=[version.pk])


def test_the_change_form_carries_the_canvas(as_staff, risk_draft):
    url = reverse("admin:state_machines_statemachineversion_change", args=[risk_draft.pk])
    response = as_staff.get(url)
    assert response.status_code == 200
    body = response.content.decode()
    assert "<state-machine-editor>" in body
    assert 'data-readonly="0"' in body
    assert machine_url(risk_draft) in body
    # The component is a module: without the script tag none of the rest matters.
    assert '<script type="module" src="/static/vinta_state_machines/admin-editor.js">' in body
    assert "/static/vinta_state_machines/admin-editor.css" in body


def test_the_vendored_component_is_installed_alongside_the_glue(as_staff):
    """The bundled build ships in the package, so a project needs no build step."""
    from django.contrib.staticfiles import finders

    assert finders.find("vinta_state_machines/state-machine-editor.js")
    assert finders.find("vinta_state_machines/admin-editor.js")


def test_a_published_version_renders_the_canvas_read_only(as_staff, risk_version):
    url = reverse("admin:state_machines_statemachineversion_change", args=[risk_version.pk])
    body = as_staff.get(url).content.decode()
    assert 'data-readonly="1"' in body
    assert "data-dsm-save" not in body


def test_getting_the_machine_returns_the_document(as_staff, risk_draft):
    response = as_staff.get(machine_url(risk_draft))
    assert response.status_code == 200
    assert response.json() == to_editor_machine(risk_draft)


def test_posting_a_machine_applies_it_and_answers_with_the_saved_document(as_staff, risk_draft):
    document = to_editor_machine(risk_draft)
    document["states"][0]["position"] = {"x": 42, "y": 7}

    response = as_staff.post(
        machine_url(risk_draft), data=json.dumps(document), content_type="application/json"
    )

    assert response.status_code == 200
    assert response.json()["states"][0]["position"] == {"x": 42, "y": 7}
    assert risk_draft.states.get(status__key="draft").x == 42


def test_a_rejected_machine_answers_with_every_reason(as_staff, risk_draft):
    document = to_editor_machine(risk_draft)
    for edge in document["transitions"]:
        edge["trigger"] = None

    response = as_staff.post(
        machine_url(risk_draft), data=json.dumps(document), content_type="application/json"
    )

    assert response.status_code == 400
    errors = response.json()["errors"]
    assert len(errors) == len(document["transitions"])
    assert all("has no trigger" in error for error in errors)


def test_posting_to_a_published_version_is_rejected(as_staff, risk_draft):
    document = to_editor_machine(risk_draft)
    publish_version(risk_draft)

    response = as_staff.post(
        machine_url(risk_draft), data=json.dumps(document), content_type="application/json"
    )

    assert response.status_code == 400
    assert "no longer be edited" in response.json()["errors"][0]


def test_malformed_json_is_rejected(as_staff, risk_draft):
    response = as_staff.post(
        machine_url(risk_draft), data="{oops", content_type="application/json"
    )
    assert response.status_code == 400
    assert "Malformed JSON" in response.json()["errors"][0]


def test_the_catalogs_are_served(as_staff, risk_draft):
    handlers = as_staff.get(
        reverse("admin:state_machines_statemachineversion_editor_side_effects")
    ).json()
    assert {entry["id"] for entry in handlers} >= {"testapp.record"}
    assert all({"id", "name", "description", "defaultParams"} <= set(entry) for entry in handlers)

    actions = as_staff.get(
        reverse("admin:state_machines_statemachineversion_editor_actions")
    ).json()
    assert {entry["id"] for entry in actions} >= {"risk.assess"}


def test_the_guard_validator_answers_both_ways(as_staff, risk_draft):
    url = reverse("admin:state_machines_statemachineversion_editor_guard")
    ok = as_staff.post(
        url, data=json.dumps({"expression": "obj.amount <= 10"}), content_type="application/json"
    )
    assert ok.json() == {"ok": True}

    bad = as_staff.post(
        url, data=json.dumps({"expression": "obj.amount <="}), content_type="application/json"
    )
    assert bad.json()["ok"] is False
    assert bad.json()["errors"]


def test_an_anonymous_visitor_is_sent_to_the_login_page(client, risk_draft):
    response = client.get(machine_url(risk_draft))
    assert response.status_code == 302
    assert "/login/" in response["Location"]


def test_a_viewer_without_change_permission_cannot_post(client, db, risk_draft):
    from django.contrib.auth.models import Permission

    viewer = User.objects.create_user(username="vi", password="x", is_staff=True)
    viewer.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="state_machines",
            codename="view_statemachineversion",
        )
    )
    client.force_login(viewer)

    assert client.get(machine_url(risk_draft)).status_code == 200
    document = copy.deepcopy(to_editor_machine(risk_draft))
    response = client.post(
        machine_url(risk_draft), data=json.dumps(document), content_type="application/json"
    )
    assert response.status_code == 404
