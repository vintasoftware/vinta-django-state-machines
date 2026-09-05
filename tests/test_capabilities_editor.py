"""The capability policy where it is actually enforced: the canvas and the admin."""

from __future__ import annotations

import copy
import json

import pytest
from django.contrib.auth.models import Permission, User
from django.urls import reverse

from vinta_state_machines.editor import (
    EditorPayloadError,
    action_catalog,
    apply_editor_machine,
    capability_errors,
    check_editor_machine,
    publish_editor_machine,
    side_effect_definitions,
    to_editor_machine,
)
from vinta_state_machines.enums import CapabilityResource, RuleEffect
from vinta_state_machines.models import (
    ActionType,
    ScopeCapabilityRule,
    StateMachineScope,
)
from vinta_state_machines.services import define_machine

pytestmark = pytest.mark.django_db

SIDE_EFFECT = CapabilityResource.SIDE_EFFECT
ACTION = CapabilityResource.ACTION


@pytest.fixture
def acme(db) -> StateMachineScope:
    scope = StateMachineScope(label="Acme")
    scope.scope = "org.1"
    scope.save()
    return scope


@pytest.fixture
def acme_draft(acme, risk_definition):
    """Acme's own risk machine, as an editable draft."""
    definition = copy.deepcopy(risk_definition)
    definition["scope"] = acme.scope_key
    return define_machine(definition)


@pytest.fixture
def acme_document(acme_draft):
    return to_editor_machine(acme_draft)


def rule(scope, resource, effect, pattern) -> ScopeCapabilityRule:
    return ScopeCapabilityRule.objects.create(
        scope=scope, resource=resource, effect=effect, pattern=pattern
    )


def edge_of(document, name):
    return next(edge for edge in document["transitions"] if edge["name"] == name)


def state_of(document, key):
    return next(state for state in document["states"] if state["id"] == key)


# ------------------------------------------------------------------- the catalogs


def test_the_side_effect_catalog_narrows_to_what_the_scope_may_use(acme):
    rule(acme, SIDE_EFFECT, RuleEffect.ALLOW, "testapp.record")

    keys = {definition["id"] for definition in side_effect_definitions(scope=acme)}

    assert keys == {"testapp.record"}


def test_the_side_effect_catalog_is_whole_for_an_unrestricted_scope(acme):
    keys = {definition["id"] for definition in side_effect_definitions(scope=acme)}

    assert "testapp.record" in keys
    assert "testapp.boom" in keys


def test_the_action_catalog_narrows_the_same_way(acme, risk_draft):
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")

    keys = {action["id"] for action in action_catalog(scope=acme)}

    assert "risk.assess" in keys
    assert "risk.discard" not in keys


def test_a_bypassing_actor_sees_the_whole_catalog(acme, risk_draft, db):
    rule(acme, ACTION, RuleEffect.DENY, "risk.*")
    root = User.objects.create_superuser(username="root", email="", password="x")

    keys = {action["id"] for action in action_catalog(scope=acme, actor=root)}

    assert "risk.discard" in keys


# ------------------------------------------------------------- the document check


def test_a_denied_trigger_is_refused_on_the_document(acme, acme_document):
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")

    errors = capability_errors(acme_document, scope=acme)

    assert len(errors) == 1
    assert "risk.discard" in errors[0]
    assert "discard" in errors[0]


def test_a_denied_side_effect_is_refused_on_the_document(acme, acme_draft, acme_document):
    rule(acme, SIDE_EFFECT, RuleEffect.DENY, "testapp.*")
    edge_of(acme_document, "assess")["effects"]["after"] = [
        {"definitionId": "testapp.record", "params": {}}
    ]

    errors = capability_errors(acme_document, scope=acme)

    assert any("testapp.record" in error for error in errors)


def test_a_denied_named_guard_is_refused_on_the_document(acme, acme_document):
    rule(acme, CapabilityResource.GUARD, RuleEffect.ALLOW, "nothing")
    edge_of(acme_document, "assess")["guard"] = "@always"

    errors = capability_errors(acme_document, scope=acme)

    assert any("guard" in error for error in errors)


def test_an_unrestricted_scope_produces_no_document_errors(acme, acme_document):
    assert capability_errors(acme_document, scope=acme) == []


def test_check_editor_machine_reports_policy_alongside_its_own_rules(acme, acme_document):
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")

    errors = check_editor_machine(acme_document, scope=acme)

    assert any("risk.discard" in error for error in errors)


def test_check_editor_machine_says_nothing_about_policy_without_a_scope(acme, acme_document):
    """The catalog endpoints and forms pass a scope; a bare call has nothing to check."""
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")

    assert check_editor_machine(acme_document) == []


# ------------------------------------------------------------------ the save path


def test_applying_a_document_with_a_denied_trigger_is_refused(acme, acme_draft, acme_document):
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")

    with pytest.raises(EditorPayloadError) as caught:
        apply_editor_machine(acme_draft, acme_document)

    assert any("risk.discard" in error for error in caught.value.errors)


def test_applying_writes_nothing_when_the_policy_refuses(acme, acme_draft, acme_document):
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")
    state_of(acme_document, "draft")["name"] = "Renamed"

    with pytest.raises(EditorPayloadError):
        apply_editor_machine(acme_draft, acme_document)

    acme_draft.refresh_from_db()
    assert to_editor_machine(acme_draft)["states"][0]["name"] != "Renamed"


def test_a_bypassing_actor_may_apply_a_denied_document(acme, acme_draft, acme_document, db):
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")
    root = User.objects.create_superuser(username="root2", email="", password="x")

    apply_editor_machine(acme_draft, acme_document, actor=root)

    assert acme_draft.transitions.filter(action_type__key="risk.discard").exists()


def test_an_unrestricted_scope_applies_as_it_always_did(acme, acme_draft, acme_document):
    apply_editor_machine(acme_draft, acme_document)

    assert acme_draft.transitions.filter(action_type__key="risk.discard").exists()


def test_the_global_policy_reaches_the_editor_too(acme, acme_draft, acme_document):
    from vinta_state_machines.scopes import get_default_scope

    rule(get_default_scope(), ACTION, RuleEffect.DENY, "risk.discard")

    with pytest.raises(EditorPayloadError):
        apply_editor_machine(acme_draft, acme_document)


# -------------------------------------------------- minting actions from the canvas


def test_an_unrestricted_scope_may_still_invent_a_trigger(acme, acme_draft, acme_document):
    """The pre-existing convenience, kept: a new verb can be born on the canvas."""
    edge_of(acme_document, "assess")["trigger"] = {"id": "risk.brand_new", "name": "Brand new"}

    apply_editor_machine(acme_draft, acme_document)

    assert ActionType.objects.filter(key="risk.brand_new").exists()


def test_a_restricted_scope_may_not_invent_a_trigger_even_inside_its_allow_list(
    acme, acme_draft, acme_document
):
    """Using the vocabulary and extending it are different privileges."""
    rule(acme, ACTION, RuleEffect.ALLOW, "risk.*")
    edge_of(acme_document, "assess")["trigger"] = {"id": "risk.brand_new", "name": "Brand new"}

    with pytest.raises(EditorPayloadError) as caught:
        apply_editor_machine(acme_draft, acme_document)

    assert any("not in the action catalog" in error for error in caught.value.errors)
    assert not ActionType.objects.filter(key="risk.brand_new").exists()


def test_a_restricted_scope_may_use_a_trigger_that_already_exists(acme, acme_draft, acme_document):
    rule(acme, ACTION, RuleEffect.ALLOW, "risk.*")
    ActionType.objects.create(key="risk.brand_new", name="Brand new")
    edge_of(acme_document, "assess")["trigger"] = {"id": "risk.brand_new", "name": "Brand new"}

    apply_editor_machine(acme_draft, acme_document)

    assert acme_draft.transitions.filter(action_type__key="risk.brand_new").exists()


def test_a_bypassing_actor_may_still_invent_a_trigger(acme, acme_draft, acme_document, db):
    rule(acme, ACTION, RuleEffect.ALLOW, "risk.*")
    root = User.objects.create_superuser(username="root3", email="", password="x")
    edge_of(acme_document, "assess")["trigger"] = {"id": "risk.brand_new", "name": "Brand new"}

    apply_editor_machine(acme_draft, acme_document, actor=root)

    assert ActionType.objects.filter(key="risk.brand_new").exists()


# ------------------------------------------------------------- publishing a canvas


def test_publishing_a_canvas_obeys_the_policy(acme, acme_draft, acme_document):
    from vinta_state_machines.services import publish_version

    publish_version(acme_draft)
    machine = acme_draft.state_machine
    document = to_editor_machine(acme_draft)
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")

    with pytest.raises(EditorPayloadError):
        publish_editor_machine(machine, document)

    assert machine.versions.count() == 1


# --------------------------------------------------------------------- the admin


@pytest.fixture
def staff(db) -> User:
    account = User.objects.create_user(username="editor", password="x", is_staff=True)
    account.user_permissions.set(
        Permission.objects.filter(content_type__app_label="state_machines").exclude(
            codename="bypass_capability_policy"
        )
    )
    return User.objects.get(pk=account.pk)


def test_the_catalog_endpoint_narrows_to_the_machine_it_is_asked_about(
    client, staff, acme, acme_draft
):
    rule(acme, SIDE_EFFECT, RuleEffect.ALLOW, "testapp.record")
    client.force_login(staff)
    url = reverse("admin:state_machines_statemachineversion_editor_side_effects")

    response = client.get(url, {"machine": acme_draft.state_machine_id})

    assert response.status_code == 200
    assert {item["id"] for item in json.loads(response.content)} == {"testapp.record"}


def test_the_catalog_endpoint_without_a_machine_is_the_whole_registry(
    client, staff, acme, acme_draft
):
    rule(acme, SIDE_EFFECT, RuleEffect.ALLOW, "testapp.record")
    client.force_login(staff)
    url = reverse("admin:state_machines_statemachineversion_editor_side_effects")

    response = client.get(url)

    assert len(json.loads(response.content)) > 1


def test_saving_a_denied_graph_over_the_editor_endpoint_is_rejected(
    client, staff, acme, acme_draft, acme_document
):
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")
    client.force_login(staff)
    url = reverse("admin:state_machines_statemachineversion_editor_machine", args=[acme_draft.pk])

    response = client.post(url, data=json.dumps(acme_document), content_type="application/json")

    assert response.status_code == 400
    assert any("risk.discard" in error for error in json.loads(response.content)["errors"])


def test_a_bypassing_staff_user_saves_the_same_graph(client, acme, acme_draft, acme_document):
    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")
    root = User.objects.create_superuser(username="root4", email="x@example.com", password="x")
    client.force_login(root)
    url = reverse("admin:state_machines_statemachineversion_editor_machine", args=[acme_draft.pk])

    response = client.post(url, data=json.dumps(acme_document), content_type="application/json")

    assert response.status_code == 200


def test_the_add_form_refuses_a_graph_the_chosen_machine_may_not_have(
    acme, acme_draft, acme_document
):
    from vinta_state_machines.forms import StateMachineVersionAddForm

    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")
    form = StateMachineVersionAddForm(
        data={
            "state_machine": acme_draft.state_machine_id,
            "version": "2",
            "notes": "",
            "graph": json.dumps(acme_document),
        }
    )

    assert not form.is_valid()
    assert any("risk.discard" in error for error in form.errors["graph"])


def test_the_add_form_accepts_the_same_graph_for_a_bypassing_actor(
    acme, acme_draft, acme_document, db
):
    from vinta_state_machines.forms import StateMachineVersionAddForm

    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")
    root = User.objects.create_superuser(username="root5", email="", password="x")

    class Bypassing(StateMachineVersionAddForm):
        actor = root

    form = Bypassing(
        data={
            "state_machine": acme_draft.state_machine_id,
            "version": "2",
            "notes": "",
            "graph": json.dumps(acme_document),
        }
    )

    assert form.is_valid(), form.errors


def test_the_machine_add_form_checks_the_scope_it_is_creating_under(acme, acme_document):
    from vinta_state_machines.forms import StateMachineWithVersionForm

    rule(acme, ACTION, RuleEffect.DENY, "risk.discard")
    form = StateMachineWithVersionForm(
        data={
            "key": "risk.other",
            "entity_type": "risk",
            "status_field": "status",
            "scope": acme.pk,
            "name": "Other",
            "description": "",
            "version": "1",
            "notes": "",
            "graph": json.dumps(acme_document),
        }
    )

    assert not form.is_valid()
    assert any("risk.discard" in error for error in form.errors["graph"])


def test_the_rule_admin_stamps_the_author(client, acme, db):
    root = User.objects.create_superuser(username="root6", email="x@example.com", password="x")
    client.force_login(root)

    response = client.post(
        reverse("admin:state_machines_scopecapabilityrule_add"),
        data={
            "scope": acme.pk,
            "resource": SIDE_EFFECT,
            "effect": RuleEffect.DENY,
            "pattern": "testapp.boom",
            "reason": "",
        },
    )

    assert response.status_code == 302
    written = ScopeCapabilityRule.objects.get()
    assert written.author is not None
    assert written.author.identity_key == str(root.pk)
