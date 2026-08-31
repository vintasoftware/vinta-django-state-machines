"""Authoring: validate, publish, clone, archive, rebase."""

from __future__ import annotations

import pytest

from tests.testapp.models import Risk
from vinta_state_machines.enums import Lifecycle
from vinta_state_machines.exceptions import InvalidVersionState
from vinta_state_machines.models import StateMachineTransition, StateMachineVersion
from vinta_state_machines.services import (
    archive_version,
    clone_version,
    define_machine,
    next_version_label,
    publish_version,
    rebase_record,
    set_default_version,
    validate_version,
)

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------ validation


def test_a_well_formed_draft_validates_clean(risk_draft):
    report = validate_version(risk_draft)
    assert report.ok and report.warnings == []


def test_a_version_with_no_initial_state_is_rejected(risk_draft):
    risk_draft.states.update(is_initial=False)
    report = validate_version(risk_draft)
    assert "The version declares no initial state." in report.errors


def test_a_version_with_no_states_is_rejected(risk_draft):
    risk_draft.transitions.all().delete()
    risk_draft.states.all().delete()
    assert "The version declares no states." in validate_version(risk_draft).errors


def test_an_outgoing_edge_from_a_terminal_state_is_rejected(risk_draft):
    risk_draft.states.filter(status__key="assessed").update(is_terminal=True)
    report = validate_version(risk_draft)
    assert any("is terminal but has the outgoing transition" in error for error in report.errors)


def test_a_creation_edge_must_target_an_initial_state(risk_draft):
    risk_draft.states.filter(status__key="draft").update(is_initial=False, is_terminal=False)
    risk_draft.states.filter(status__key="assessed").update(is_initial=True)
    report = validate_version(risk_draft)
    assert any("is not marked initial" in error for error in report.errors)


def test_an_unparsable_guard_is_rejected(risk_draft):
    risk_draft.transitions.filter(action_type__key="risk.assess").update(guard="obj.amount ===")
    assert any("unusable" in error for error in validate_version(risk_draft).errors)


def test_a_guard_naming_an_unregistered_function_is_rejected(risk_draft):
    risk_draft.transitions.filter(action_type__key="risk.assess").update(guard="@nope")
    assert any("unusable" in error for error in validate_version(risk_draft).errors)


def test_a_hook_naming_an_unregistered_handler_is_rejected(risk_draft):
    from vinta_state_machines.models import StateMachineHook

    StateMachineHook.objects.create(
        state_machine_version=risk_draft, handler_key="nope", event="any_transition"
    )
    assert any(
        "no installed app registers" in error for error in validate_version(risk_draft).errors
    )


def test_an_unreachable_state_is_a_warning_not_an_error(risk_draft):
    orphan = risk_draft.states.get(status__key="rejected")
    StateMachineTransition.objects.filter(to_state=orphan).delete()
    report = validate_version(risk_draft)
    assert report.ok
    assert "State rejected is unreachable." in report.warnings


def test_a_dead_end_that_is_not_marked_terminal_is_a_warning(risk_draft):
    risk_draft.states.filter(status__key="mitigated").update(is_terminal=False)
    report = validate_version(risk_draft)
    assert report.ok
    assert any("not marked terminal" in warning for warning in report.warnings)


# ------------------------------------------------------------------ publishing


def test_publishing_freezes_the_draft_and_makes_it_the_default(risk_draft):
    publish_version(risk_draft)
    risk_draft.refresh_from_db()
    assert risk_draft.lifecycle == Lifecycle.PUBLISHED
    assert risk_draft.published_at is not None
    assert risk_draft.state_machine.default_version_id == risk_draft.pk


def test_publishing_can_leave_the_default_alone(risk_version, risk_machine):
    clone = clone_version(risk_version, "2")
    publish_version(clone, make_default=False)
    risk_machine.refresh_from_db()
    assert risk_machine.default_version_id == risk_version.pk


def test_an_invalid_draft_refuses_to_publish_and_says_why(risk_draft):
    risk_draft.states.update(is_initial=False)
    with pytest.raises(InvalidVersionState, match="declares no initial state"):
        publish_version(risk_draft)
    risk_draft.refresh_from_db()
    assert risk_draft.lifecycle == Lifecycle.DRAFT


def test_only_a_draft_can_be_published(risk_version):
    with pytest.raises(InvalidVersionState, match="Only drafts can be published"):
        publish_version(risk_version)


def test_publishing_returns_the_warnings_it_did_not_block_on(risk_draft):
    risk_draft.states.filter(status__key="mitigated").update(is_terminal=False)
    report = publish_version(risk_draft)
    assert report.ok and report.warnings


def test_only_a_published_version_can_become_the_default(risk_machine, risk_version):
    draft = clone_version(risk_version, "2")
    with pytest.raises(InvalidVersionState, match="only a published"):
        set_default_version(risk_machine, draft)


# --------------------------------------------------------------------- cloning


def test_cloning_deep_copies_states_transitions_and_hooks(risk_version):
    from vinta_state_machines.models import StateMachineHook

    StateMachineHook.objects.create(
        state_machine_version=risk_version,
        handler_key="testapp.record",
        event="enter_state",
        state=risk_version.states.get(status__key="assessed"),
    )
    clone = clone_version(risk_version, "2", notes="second cut")

    assert clone.lifecycle == Lifecycle.DRAFT
    assert clone.states.count() == risk_version.states.count()
    assert clone.transitions.count() == risk_version.transitions.count()
    assert clone.hooks.count() == 1
    # Copies, not shared rows.
    assert not set(clone.states.values_list("pk", flat=True)) & set(
        risk_version.states.values_list("pk", flat=True)
    )
    assert clone.hooks.get().state.state_machine_version_id == clone.pk


def test_editing_a_clone_leaves_the_original_untouched(risk_version):
    clone = clone_version(risk_version, "2")
    clone.states.filter(status__key="rejected").delete()
    assert risk_version.states.filter(status__key="rejected").exists()


def test_a_clone_reuses_the_shared_vocabulary(risk_version):
    clone = clone_version(risk_version, "2")
    assert set(clone.states.values_list("status_id", flat=True)) == set(
        risk_version.states.values_list("status_id", flat=True)
    )


# ------------------------------------------------------------------- archiving


def test_archiving_the_default_version_requires_a_replacement(risk_machine, risk_version):
    with pytest.raises(InvalidVersionState, match="pass replacement"):
        archive_version(risk_version)


def test_archiving_hands_the_default_over_to_the_replacement(risk_machine, risk_version):
    replacement = clone_version(risk_version, "2")
    publish_version(replacement, make_default=False)
    archive_version(risk_version, replacement=replacement)

    risk_version.refresh_from_db()
    risk_machine.refresh_from_db()
    assert risk_version.lifecycle == Lifecycle.ARCHIVED
    assert risk_machine.default_version_id == replacement.pk


def test_an_archived_version_still_protects_the_records_that_pinned_it(risk_machine, risk):
    replacement = clone_version(risk_machine.default_version, "2")
    publish_version(replacement, make_default=False)
    archive_version(risk_machine.default_version, replacement=replacement)
    risk.refresh_from_db()
    assert risk.status_machine_version.lifecycle == Lifecycle.ARCHIVED


# -------------------------------------------------------------------- rebasing


def test_rebasing_is_the_deliberate_way_to_move_a_record_forward(risk_machine, risk):
    old = risk.status_machine_version
    clone = clone_version(old, "2")
    publish_version(clone)

    rebase_record(risk, clone)
    risk.refresh_from_db()
    assert risk.status_machine_version_id == clone.pk
    assert risk.status_key == "draft"


def test_rebasing_refuses_when_the_current_status_is_gone(risk_machine, risk):
    clone = clone_version(risk.status_machine_version, "2")
    clone.transitions.filter(from_state__status__key="draft").delete()
    clone.transitions.filter(to_state__status__key="draft").delete()
    clone.states.filter(status__key="draft").delete()
    with pytest.raises(InvalidVersionState, match="pass map_status"):
        rebase_record(risk, clone)


def test_a_renamed_status_is_carried_across_by_map_status(risk_machine, risk):
    definition = {
        "key": "risk.status",
        "entity_type": "risk",
        "status_field": "status",
        "name": "Risk status",
        "version": "2",
        "states": [
            {"key": "new", "name": "New", "is_initial": True},
            {"key": "assessed", "name": "Assessed", "is_terminal": True},
        ],
        "transitions": [{"from": "new", "to": "assessed", "action": "risk.assess"}],
    }
    version_two = define_machine(definition)
    publish_version(version_two)

    rebase_record(risk, version_two, map_status={"draft": "new"})
    risk.refresh_from_db()
    assert (risk.status_key, risk.status_machine_version_id) == ("new", version_two.pk)


def test_rebasing_onto_another_machine_is_refused(risk, risk_version):
    other = define_machine(
        {
            "key": "roadmap.status",
            "entity_type": "roadmap",
            "status_field": "status",
            "name": "Roadmap status",
            "version": "1",
            "states": [{"key": "draft", "name": "Draft", "is_initial": True}],
        }
    )
    with pytest.raises(InvalidVersionState, match="governs"):
        rebase_record(risk, other)


# ------------------------------------------------------------ declarative build


def test_define_machine_creates_the_vocabulary_it_needs(risk_draft):
    from vinta_state_machines.models import ActionType, StatusDefinition

    assert StatusDefinition.objects.filter(entity_type="risk", status_field="status").count() == 4
    assert ActionType.objects.filter(key__startswith="risk.").count() == 5


def test_define_machine_is_idempotent_about_the_machine_itself(risk_definition):
    define_machine(risk_definition)
    risk_definition["version"] = "2"
    second = define_machine(risk_definition)
    assert StateMachineVersion.objects.filter(state_machine=second.state_machine).count() == 2


def test_a_hook_naming_a_transition_the_version_lacks_is_refused(risk_definition):
    risk_definition["hooks"] = [{"handler": "testapp.record", "transition": "nope"}]
    with pytest.raises(InvalidVersionState, match="does not declare"):
        define_machine(risk_definition)


def test_a_hook_bound_to_a_transition_must_name_one(risk_definition):
    risk_definition["hooks"] = [{"handler": "testapp.record", "event": "transition"}]
    with pytest.raises(InvalidVersionState, match="does not name one"):
        define_machine(risk_definition)


def test_a_defined_hook_is_wired_to_the_right_edge(risk_definition):
    risk_definition["hooks"] = [
        {"handler": "testapp.record", "transition": "assess", "timing": "before"}
    ]
    version = define_machine(risk_definition)
    hook = version.hooks.get()
    assert hook.transition.name == "assess"
    assert hook.transition.action_type.key == "risk.assess"
    assert hook.timing == "before"


def test_a_hook_naming_an_edge_that_leaves_two_states_asks_which(risk_definition):
    """Names are unique per source state, so the same name may appear twice."""
    risk_definition["transitions"].append(
        {"name": "assess", "from": "assessed", "to": "assessed", "action": "risk.reassess"}
    )
    risk_definition["hooks"] = [{"handler": "testapp.record", "transition": "assess"}]
    with pytest.raises(InvalidVersionState, match="leaves more than one state"):
        define_machine(risk_definition)

    risk_definition["hooks"] = [
        {"handler": "testapp.record", "transition": "assess", "from": "assessed"}
    ]
    version = define_machine(risk_definition)
    assert version.hooks.get().transition.from_state.status.key == "assessed"


def test_a_hook_params_json_is_stored_on_the_relationship(risk_definition):
    risk_definition["hooks"] = [
        {"handler": "testapp.record", "transition": "assess", "params": {"label": "x", "n": 3}}
    ]
    version = define_machine(risk_definition)
    assert version.hooks.get().params == {"label": "x", "n": 3}


def test_a_record_created_on_a_new_version_starts_where_that_version_says(risk_version):
    clone = clone_version(risk_version, "2")
    clone.states.filter(status__key="draft").update(is_initial=False)
    clone.states.filter(status__key="assessed").update(is_initial=True)
    clone.transitions.filter(from_state__isnull=True).delete()
    publish_version(clone, validate=False)

    fresh = Risk.objects.create(title="Straight to assessed")
    assert fresh.status_key == "assessed"


# ------------------------------------------------------------- next version label


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        ("1", "2"),
        ("9", "10"),
        ("2024.1", "2024.2"),
        ("v3", "v4"),
        ("draft", "draft-2"),
        ("", "-2"),
    ],
)
def test_the_next_label_bumps_the_trailing_number(risk_machine, after, expected):
    assert next_version_label(risk_machine, after=after) == expected


def test_the_next_label_follows_the_latest_version_by_default(risk_machine, risk_version):
    assert next_version_label(risk_machine) == "2"
    clone_version(risk_version, "2")
    assert next_version_label(risk_machine) == "3"


def test_the_next_label_skips_labels_already_taken(risk_machine, risk_version):
    """Versions are not always numbered in order, so a free label is searched for."""
    clone_version(risk_version, "2")
    clone_version(risk_version, "3")
    assert next_version_label(risk_machine, after="1") == "4"


def test_the_first_version_of_an_empty_machine_is_labelled_one(risk_machine):
    risk_machine.default_version = None
    risk_machine.save(update_fields=["default_version"])
    risk_machine.versions.all().delete()
    assert next_version_label(risk_machine) == "1"


def test_the_latest_version_is_the_newest_whatever_its_lifecycle(risk_machine, risk_version):
    assert risk_machine.latest_version() == risk_version
    draft = clone_version(risk_version, "2")
    assert risk_machine.latest_version() == draft
