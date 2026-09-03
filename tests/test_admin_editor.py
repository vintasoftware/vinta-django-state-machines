"""The canvas endpoints hanging off a version's change form."""

from __future__ import annotations

import copy
import json
import re
import struct
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from django.utils import translation

import vinta_state_machines
from vinta_state_machines.editor import to_editor_machine
from vinta_state_machines.enums import HookEvent, Lifecycle
from vinta_state_machines.models import StateMachine, StateMachineHook, StateMachineVersion
from vinta_state_machines.scopes import scope_from_key
from vinta_state_machines.services import clone_version, publish_version

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
    # Imported by the glue, so a missing one takes the whole canvas with it.
    assert finders.find("vinta_state_machines/editor-strings.js")


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


# ------------------------------------------------- the canvas on a machine's own form
#
# A version's canvas edits that one draft in place.  A machine's edits the flow and
# publishes a new version on every save — which is what the block below tells apart.


def machine_editor_url(machine) -> str:
    return reverse("admin:state_machines_statemachine_editor_machine", args=[machine.pk])


def blank_machine(key: str = "thing.status") -> StateMachine:
    """A machine with no versions at all."""
    return StateMachine.objects.create(
        key=key,
        entity_type="thing",
        status_field="status",
        scope=scope_from_key(None),
        name="Thing status",
    )


def one_state_document(machine_key: str, *, version: Any = None) -> dict[str, Any]:
    """The smallest publishable document: one initial state and the edge into it."""
    return {
        "states": [
            {
                "id": "state_1",
                "name": "Open",
                "position": {"x": 10, "y": 20},
                "color": "neutral",
                "description": "",
                "onEnter": {"before": [], "after": []},
                "onLeave": {"before": [], "after": []},
                "data": {},
            }
        ],
        "transitions": [
            {
                "id": "transition_1",
                "name": "create",
                "from": None,
                "to": "state_1",
                "trigger": {"id": "thing.create", "name": "Create"},
                "guard": "",
                "requiredPermission": "",
                "description": "",
                "labelOffset": {"x": 0, "y": 0},
                "effects": {"before": [], "after": []},
                "data": {},
            }
        ],
        "initialStateIds": ["state_1"],
        "finalStateIds": [],
        "data": {"machine": machine_key, "version": version},
    }


def test_the_machine_change_form_carries_an_editable_canvas(as_staff, risk_machine):
    url = reverse("admin:state_machines_statemachine_change", args=[risk_machine.pk])
    body = as_staff.get(url).content.decode()

    assert "<state-machine-editor>" in body
    # Editable even though the version it draws is published: saving writes a new one.
    assert 'data-readonly="0"' in body
    assert "Save and publish a new version" in body
    # The rest of the page goes stale on save, so this form reloads itself.
    assert 'data-reload-on-save="1"' in body
    assert machine_editor_url(risk_machine) in body


def test_the_machine_canvas_opens_on_the_latest_version(as_staff, risk_machine, risk_version):
    response = as_staff.get(machine_editor_url(risk_machine))
    assert response.status_code == 200
    assert response.json() == to_editor_machine(risk_version)


def test_the_machine_canvas_prefers_a_newer_draft_to_the_published_default(
    as_staff, risk_machine, risk_version
):
    """The newest picture of the flow is the latest version, pinned or not."""
    draft = clone_version(risk_version, "2")
    assert as_staff.get(machine_editor_url(risk_machine)).json()["data"]["version"] == "2"
    assert draft.lifecycle == Lifecycle.DRAFT


def test_saving_the_machine_canvas_publishes_a_new_version(as_staff, risk_machine, risk_version):
    document = copy.deepcopy(to_editor_machine(risk_version))
    document["states"][0]["position"] = {"x": 42, "y": 7}

    response = as_staff.post(
        machine_editor_url(risk_machine),
        data=json.dumps(document),
        content_type="application/json",
    )

    assert response.status_code == 200
    published = StateMachineVersion.objects.get(version="2")
    assert published.lifecycle == Lifecycle.PUBLISHED
    assert published.published_at is not None
    assert published.states.get(status__key="draft").x == 42
    # The response is the new version's document, not the one that was posted.
    assert response.json()["data"]["version"] == "2"
    assert response.json()["states"][0]["position"] == {"x": 42, "y": 7}


def test_publishing_from_the_canvas_moves_the_default_and_leaves_the_old_graph_alone(
    as_staff, risk_machine, risk_version
):
    document = copy.deepcopy(to_editor_machine(risk_version))
    document["states"] = [state for state in document["states"] if state["id"] != "rejected"]
    document["transitions"] = [
        edge for edge in document["transitions"] if edge["to"] != "rejected"
    ]

    response = as_staff.post(
        machine_editor_url(risk_machine),
        data=json.dumps(document),
        content_type="application/json",
    )
    assert response.status_code == 200

    published = StateMachineVersion.objects.get(version="2")
    risk_machine.refresh_from_db()
    risk_version.refresh_from_db()
    assert risk_machine.default_version_id == published.pk
    assert not published.states.filter(status__key="rejected").exists()
    # Records pinned version 1, so its graph is exactly what it always was.
    assert risk_version.lifecycle == Lifecycle.PUBLISHED
    assert risk_version.states.count() == 4
    assert risk_version.transitions.count() == 5


def test_publishing_from_the_canvas_records_who_did_it(
    as_staff, staff, risk_machine, risk_version
):
    document = copy.deepcopy(to_editor_machine(risk_version))
    as_staff.post(
        machine_editor_url(risk_machine),
        data=json.dumps(document),
        content_type="application/json",
    )

    published = StateMachineVersion.objects.get(version="2")
    assert published.author is not None
    assert published.author.user_id == staff.pk


def test_hooks_the_canvas_cannot_draw_survive_a_save(as_staff, risk_machine, risk_version):
    """``any_transition`` hooks belong to the version, not to a card, so they are copied."""
    StateMachineHook.objects.create(
        state_machine_version=risk_version,
        handler_key="testapp.record",
        event=HookEvent.ANY_TRANSITION,
        params={"label": "everything"},
    )
    document = copy.deepcopy(to_editor_machine(risk_version))

    as_staff.post(
        machine_editor_url(risk_machine),
        data=json.dumps(document),
        content_type="application/json",
    )

    carried = StateMachineVersion.objects.get(version="2").hooks.get(
        event=HookEvent.ANY_TRANSITION
    )
    assert carried.handler_key == "testapp.record"
    assert carried.params == {"label": "everything"}


def test_a_canvas_loaded_from_a_superseded_version_is_refused(
    as_staff, risk_machine, risk_version
):
    """Two tabs must not silently overwrite each other's published work."""
    stale = copy.deepcopy(to_editor_machine(risk_version))
    as_staff.post(
        machine_editor_url(risk_machine),
        data=json.dumps(copy.deepcopy(stale)),
        content_type="application/json",
    )

    response = as_staff.post(
        machine_editor_url(risk_machine),
        data=json.dumps(stale),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "Reload before saving" in response.json()["errors"][0]
    assert StateMachineVersion.objects.count() == 2


def test_an_unpublishable_graph_writes_no_version_at_all(as_staff, risk_machine, risk_version):
    document = copy.deepcopy(to_editor_machine(risk_version))
    document["initialStateIds"] = []

    response = as_staff.post(
        machine_editor_url(risk_machine),
        data=json.dumps(document),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert any("no initial state" in error for error in response.json()["errors"])
    assert list(StateMachineVersion.objects.values_list("version", flat=True)) == ["1"]


def test_a_machine_with_no_versions_opens_on_a_blank_canvas(as_staff):
    machine = blank_machine()
    response = as_staff.get(machine_editor_url(machine))

    assert response.status_code == 200
    assert response.json()["states"] == []
    assert response.json()["data"] == {"machine": "thing.status"}


def test_a_machine_with_no_versions_gets_its_first_one_from_the_canvas(as_staff):
    machine = blank_machine()

    response = as_staff.post(
        machine_editor_url(machine),
        data=json.dumps(one_state_document(machine.key)),
        content_type="application/json",
    )

    assert response.status_code == 200
    version = machine.versions.get()
    machine.refresh_from_db()
    assert version.version == "1"
    assert version.lifecycle == Lifecycle.PUBLISHED
    assert machine.default_version_id == version.pk
    # A state drawn on the canvas is given a real vocabulary key, slugified from its name.
    assert version.states.get().status.key == "open"


def test_a_document_belonging_to_another_machine_is_refused(as_staff, risk_machine):
    response = as_staff.post(
        machine_editor_url(risk_machine),
        data=json.dumps(one_state_document("somebody.else", version="1")),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert StateMachineVersion.objects.count() == 1


def test_a_viewer_without_change_permission_cannot_publish_from_the_machine_canvas(
    client, db, risk_machine, risk_version
):
    from django.contrib.auth.models import Permission

    viewer = User.objects.create_user(username="vi2", password="x", is_staff=True)
    viewer.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="state_machines", codename="view_statemachine"
        )
    )
    client.force_login(viewer)

    assert client.get(machine_editor_url(risk_machine)).status_code == 200
    response = client.post(
        machine_editor_url(risk_machine),
        data=json.dumps(copy.deepcopy(to_editor_machine(risk_version))),
        content_type="application/json",
    )
    assert response.status_code == 404
    assert StateMachineVersion.objects.count() == 1


# ------------------------------------------------- the canvas in the admin's language
#
# The component ships English and takes no view on locales, so the labels are marked in
# `editor-strings.js` and served as a ``djangojs`` catalog beside the other endpoints.
# What follows walks that path end to end: a project's own translation, the catalog it
# comes out of, and the strings file that reads it.


STRINGS_JS = (
    Path(vinta_state_machines.__file__).parent / "static/vinta_state_machines/editor-strings.js"
)
BUNDLE_JS = (
    Path(vinta_state_machines.__file__).parent
    / "static/vinta_state_machines/state-machine-editor.js"
)

#: Every group of `EditorStrings`, which the file below has to name exactly.
STRING_GROUPS = frozenset(
    {
        "toolbar",
        # 0.9.0: the decision card, the waiting band, and the advisory stripes.
        "decision",
        "waiting",
        "issue",
        "canvas",
        "kind",
        "card",
        "state",
        "color",
        "rename",
        "transition",
        "startNode",
        "source",
        "chip",
        "phase",
        "trigger",
        "triggerVerb",
        "sideEffect",
        "sideEffects",
        "row",
        "params",
        "properties",
        "change",
        "dialog",
        "organize",
        "json",
        "seed",
    }
)


def i18n_url() -> str:
    return reverse("admin:state_machines_statemachineversion_editor_i18n")


def write_mo(path: Path, catalog: dict[str, str]) -> None:
    """A compiled catalog, so the test needs no gettext toolchain to make one.

    Plural entries are the gettext convention: ``singular\\0plural`` as the key,
    the forms joined the same way as the value.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = sorted(catalog)
    ids = b"".join(key.encode() + b"\0" for key in keys)
    strings = b"".join(catalog[key].encode() + b"\0" for key in keys)
    id_table_offset = 7 * 4 + 16 * len(keys)
    string_table_offset = id_table_offset + len(ids)

    tables = b""
    id_at, string_at = 0, 0
    for key in keys:
        msgid = key.encode()
        tables += struct.pack("<II", len(msgid), id_table_offset + id_at)
        id_at += len(msgid) + 1
    for key in keys:
        msgstr = catalog[key].encode()
        tables += struct.pack("<II", len(msgstr), string_table_offset + string_at)
        string_at += len(msgstr) + 1

    header = struct.pack("<IIIIIII", 0x950412DE, 0, len(keys), 7 * 4, 7 * 4 + 8 * len(keys), 0, 0)
    path.write_bytes(header + tables + ids + strings)


def test_the_change_form_loads_the_catalog_before_the_glue(as_staff, risk_draft):
    """Order matters: the glue reads `django.gettext` as it labels the canvas."""
    url = reverse("admin:state_machines_statemachineversion_change", args=[risk_draft.pk])
    body = as_staff.get(url).content.decode()
    catalog = body.index(f'<script src="{i18n_url()}">')
    glue = body.index("/static/vinta_state_machines/admin-editor.js")
    assert catalog < glue


def test_the_catalog_is_served_as_a_gettext_library(as_staff):
    response = as_staff.get(i18n_url())
    assert response.status_code == 200
    assert "javascript" in response["Content-Type"]
    body = response.content.decode()
    # What `editor-strings.js` calls, and the plural rule that makes it worth serving.
    assert "django.gettext" in body
    assert "django.ngettext" in body
    assert "django.pluralidx" in body


def test_an_anonymous_visitor_cannot_read_the_catalog(client):
    response = client.get(i18n_url())
    assert response.status_code == 302
    assert "/login/" in response["Location"]


def test_a_project_translation_reaches_the_canvas(as_staff, tmp_path):
    """A ``djangojs`` catalog on ``LOCALE_PATHS`` is what a project actually writes."""
    header = "Content-Type: text/plain; charset=UTF-8\nPlural-Forms: nplurals=2; plural=(n > 1);\n"
    write_mo(
        tmp_path / "fr" / "LC_MESSAGES" / "djangojs.mo",
        {
            "": header,
            "Add state": "Ajouter un état",
            "%(count)s item\0%(count)s items": "%(count)s élément\0%(count)s éléments",
        },
    )
    with override_settings(LOCALE_PATHS=[str(tmp_path)]), translation.override("fr"):
        body = as_staff.get(i18n_url()).content.decode()

    # The catalog rides inside the script as JSON, so anything non-ASCII is escaped.
    def as_json(text: str) -> str:
        return json.dumps(text)[1:-1]

    assert as_json("Ajouter un état") in body
    # Both forms travel, and the language's own rule picks between them in the browser.
    assert as_json("%(count)s élément") in body
    assert as_json("%(count)s éléments") in body
    assert "(n > 1)" in body


def test_every_string_the_glue_names_exists_in_the_component():
    """A key the component does not have is ignored in silence, so check them here."""
    text = STRINGS_JS.read_text(encoding="utf-8")
    # The table itself, so the helpers above it are not mistaken for labels.
    source = text[text.index("export const editorStrings") :]
    bundle = BUNDLE_JS.read_text(encoding="utf-8")

    groups = set(re.findall(r"^  ([A-Za-z]+): \{$", source, re.MULTILINE))
    assert groups == STRING_GROUPS

    keys = set(re.findall(r"^    '?([A-Za-z][\w-]*)'?:", source, re.MULTILINE))
    assert len(keys) > 100, "the table lost most of itself"
    unknown = {key for key in keys if key not in bundle}
    assert not unknown, f"not in the vendored component: {sorted(unknown)}"


def test_the_canvas_says_where_a_fan_out_link_goes(as_staff, risk_machine):
    """The canvas draws one machine; a fan-out crosses into another.

    The component only announces where the user wants to be, so without this the
    FANS OUT TO link in a waiting state's band has nowhere to take them.
    """
    response = as_staff.get(
        reverse("admin:state_machines_statemachine_change", args=[risk_machine.pk])
    )

    body = response.content.decode()
    assert 'data-machines-url="/admin/state_machines/statemachine/"' in body
