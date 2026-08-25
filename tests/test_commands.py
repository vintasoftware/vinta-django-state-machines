"""The management commands that move definitions in and out of the database."""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from vinta_state_machines.enums import Lifecycle
from vinta_state_machines.models import StateMachine, StateMachineVersion

pytestmark = pytest.mark.django_db


def run(*args, **kwargs) -> str:
    out = StringIO()
    call_command(*args, stdout=out, **kwargs)
    return out.getvalue()


# ------------------------------------------------------------------- importing


def test_importing_creates_a_draft(tmp_path, risk_definition):
    path = tmp_path / "risk.json"
    path.write_text(json.dumps(risk_definition))
    output = run("import_state_machine", str(path))

    version = StateMachineVersion.objects.get()
    assert version.lifecycle == Lifecycle.DRAFT
    assert version.states.count() == 4
    assert "Created draft" in output


def test_importing_with_publish_makes_it_the_default(tmp_path, risk_definition):
    path = tmp_path / "risk.json"
    path.write_text(json.dumps(risk_definition))
    run("import_state_machine", str(path), publish=True)

    machine = StateMachine.objects.get(key="risk.status")
    assert machine.default_version.lifecycle == Lifecycle.PUBLISHED


def test_importing_a_list_creates_every_machine_in_it(tmp_path, risk_definition):
    second = dict(risk_definition, key="roadmap.status", entity_type="roadmap")
    path = tmp_path / "machines.json"
    path.write_text(json.dumps([risk_definition, second]))
    run("import_state_machine", str(path))
    assert StateMachine.objects.count() == 2


def test_a_missing_file_is_reported_cleanly(tmp_path):
    with pytest.raises(CommandError, match="Cannot read"):
        run("import_state_machine", str(tmp_path / "nope.json"))


def test_malformed_json_is_reported_cleanly(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(CommandError, match="not valid JSON"):
        run("import_state_machine", str(path))


# ------------------------------------------------------------------- exporting


def test_exporting_round_trips_through_import(tmp_path, risk_version, risk_definition):
    exported = json.loads(run("export_state_machine", "risk.status"))

    assert exported["key"] == "risk.status"
    assert [state["key"] for state in exported["states"]] == [
        state["key"] for state in risk_definition["states"]
    ]
    assert {(edge["from"], edge["to"], edge["action"]) for edge in exported["transitions"]} == {
        (edge.get("from"), edge["to"], edge["action"]) for edge in risk_definition["transitions"]
    }

    StateMachineVersion.objects.all().delete()
    StateMachine.objects.all().delete()
    path = tmp_path / "round-trip.json"
    path.write_text(json.dumps(exported))
    run("import_state_machine", str(path))
    assert StateMachineVersion.objects.get().states.count() == 4


def test_exporting_carries_guards_and_permissions_across(risk_version):
    exported = json.loads(run("export_state_machine", "risk.status"))
    by_action = {edge["action"]: edge for edge in exported["transitions"]}
    assert by_action["risk.mitigate"]["guard"] == "obj.amount <= 1000"
    assert by_action["risk.reject"]["required_permission"] == "testapp.change_risk"
    assert by_action["risk.discard"]["requires_approval"] is True


def test_exporting_includes_hooks(risk_version):
    from vinta_state_machines.models import StateMachineHook

    StateMachineHook.objects.create(
        state_machine_version=risk_version,
        handler_key="testapp.record",
        event="enter_state",
        state=risk_version.states.get(status__key="assessed"),
        params={"template": "assessed"},
    )
    exported = json.loads(run("export_state_machine", "risk.status"))
    assert exported["hooks"] == [
        {
            "handler": "testapp.record",
            "timing": "after",
            "event": "enter_state",
            "transition": None,
            "from": None,
            "state": "assessed",
            "params": {"template": "assessed"},
            "order": 0,
            "on_commit": False,
            "is_active": True,
        }
    ]


def test_exporting_to_a_file_writes_it(tmp_path, risk_version):
    path = tmp_path / "out.json"
    run("export_state_machine", "risk.status", output=path)
    assert json.loads(path.read_text())["key"] == "risk.status"


def test_exporting_an_unknown_machine_is_reported(risk_version):
    with pytest.raises(CommandError, match="No state machine with key"):
        run("export_state_machine", "nope")


def test_exporting_an_unknown_version_is_reported(risk_version):
    with pytest.raises(CommandError, match="has no version '9'"):
        run("export_state_machine", "risk.status", label="9")


def test_exporting_a_machine_with_no_default_asks_for_a_version(risk_machine, risk_version):
    risk_machine.default_version = None
    risk_machine.save(update_fields=["default_version"])
    with pytest.raises(CommandError, match="pass --label"):
        run("export_state_machine", "risk.status")


# ------------------------------------------------------------------ validating


def test_validating_a_good_catalog_reports_ok(risk_version):
    assert "ok" in run("validate_state_machines")


def test_validating_surfaces_errors_and_fails(risk_draft):
    risk_draft.states.update(is_initial=False)
    with pytest.raises(CommandError, match="1 version\\(s\\) failed"):
        run("validate_state_machines")


def test_warnings_alone_do_not_fail_unless_asked(risk_draft):
    risk_draft.states.filter(status__key="mitigated").update(is_terminal=False)
    assert "not marked terminal" in run("validate_state_machines")
    with pytest.raises(CommandError):
        run("validate_state_machines", fail_on_warning=True)


def test_validation_can_be_narrowed_to_one_machine_and_lifecycle(risk_version):
    assert run("validate_state_machines", machine="risk.status", lifecycle="published")
    assert run("validate_state_machines", machine="nothing.here") == ""


def test_the_registered_side_effects_can_be_listed(risk_version):
    output = run("validate_state_machines", list_side_effects=True)
    assert "testapp.record" in output
