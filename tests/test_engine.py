"""What a record may do, and what happens when it does it."""

from __future__ import annotations

import pytest

from tests.testapp.models import Risk, Unpinned
from vinta_state_machines.engine import (
    available_actions,
    available_transitions,
    can_transition,
    current_state,
    initial_status_key,
    resolve_version,
    transition,
)
from vinta_state_machines.enums import IdentityType
from vinta_state_machines.exceptions import (
    ApprovalRequired,
    GuardFailed,
    InvalidVersionState,
    NoStateMachineVersion,
    PermissionDenied,
    TransitionNotAllowed,
    UnknownStatus,
)
from vinta_state_machines.models import StatusTransition
from vinta_state_machines.services import clone_version, publish_version

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------- resolution


def test_a_new_record_pins_the_default_version_and_lands_on_the_initial_state(risk_version):
    risk = Risk.objects.create(title="Backups", amount=10)
    assert risk.status_key == "draft"
    assert risk.status_machine_version_id == risk_version.pk


def test_an_explicit_status_is_respected_on_creation(risk_version):
    risk = Risk.objects.create(title="Backups", status_key="assessed")
    assert risk.status_key == "assessed"
    assert risk.status_machine_version_id == risk_version.pk


def test_autopin_can_be_turned_off_per_field(risk_version):
    record = Unpinned.objects.create()
    assert record.status_key == ""
    assert record.status_machine_version_id is None


def test_an_unsaved_record_still_resolves_the_default_version(risk_version):
    assert resolve_version(Risk(title="Fresh")).pk == risk_version.pk


def test_resolving_without_a_default_version_is_an_error(risk_machine, risk_version):
    risk_machine.default_version = None
    risk_machine.save(update_fields=["default_version"])
    with pytest.raises(NoStateMachineVersion, match="no default_version"):
        resolve_version(Risk(title="Fresh"))


def test_current_state_exposes_the_version_specific_presentation(risk):
    state = current_state(risk)
    assert state.key == "draft"
    assert state.is_initial and not state.is_terminal


def test_initial_status_key_reads_the_lowest_ordered_initial_state(risk_version):
    assert initial_status_key(Risk) == "draft"


# ------------------------------------------------------------------ inspection


def test_available_transitions_lists_only_what_this_caller_can_do(risk, user):
    actions = {item.action for item in available_transitions(risk, actor=user)}
    assert actions == {"risk.assess"}


def test_blocked_transitions_can_be_listed_with_their_reason(risk, user):
    blocked = {
        item.action: item.reason
        for item in available_transitions(risk, actor=user, include_blocked=True)
        if not item.allowed
    }
    assert blocked == {"risk.discard": "requires approval"}


def test_a_terminal_state_offers_nothing(risk, privileged_user):
    transition(risk, "risk.assess")
    transition(risk, "risk.reject", actor=privileged_user)
    assert available_actions(risk, actor=privileged_user) == []


def test_can_transition_is_true_for_an_edge_that_only_awaits_approval(risk):
    assert can_transition(risk, "risk.discard") is True


def test_can_transition_is_false_for_an_edge_the_version_does_not_declare(risk):
    assert can_transition(risk, "risk.mitigate") is False


def test_an_unknown_status_is_reported_rather_than_silently_ignored(risk):
    risk.status_key = "invented"
    with pytest.raises(UnknownStatus, match="invented"):
        available_transitions(risk)


# ------------------------------------------------------------------ executing


def test_a_transition_moves_the_record_and_persists_it(risk):
    transition(risk, "risk.assess")
    risk.refresh_from_db()
    assert risk.status_key == "assessed"


def test_a_transition_writes_one_history_row(risk, user):
    record = transition(risk, "risk.assess", actor=user, comment="reviewed")
    assert isinstance(record, StatusTransition)
    assert record.from_status.key == "draft"
    assert record.to_status.key == "assessed"
    assert record.action_type.key == "risk.assess"
    assert record.state_machine_version_id == risk.status_machine_version_id
    assert record.actor_id == user.pk
    assert record.comment == "reviewed"
    assert record.target == risk


def test_history_records_the_exact_edge_that_was_taken(risk):
    record = transition(risk, "risk.assess")
    assert record.transition.name == "assess"
    assert record.transition.state_machine_version_id == risk.status_machine_version_id


def test_history_records_the_status_field_the_catalog_governs(risk):
    record = transition(risk, "risk.assess")
    assert record.status_field == "status"


def test_an_undeclared_action_is_refused_and_lists_what_is_available(risk):
    with pytest.raises(TransitionNotAllowed, match="Available from here: risk.assess"):
        transition(risk, "risk.mitigate")


def test_a_terminal_state_has_nowhere_to_go(risk, privileged_user):
    transition(risk, "risk.assess")
    transition(risk, "risk.reject", actor=privileged_user)
    with pytest.raises(TransitionNotAllowed, match="Available from here: <none>"):
        transition(risk, "risk.assess")


def test_a_terminal_state_refuses_even_an_edge_a_bad_version_declares(risk_version, risk):
    """Validation rejects such a graph, so this is the engine's own last line of defence."""
    from vinta_state_machines.models import StateMachineTransition

    mitigated = risk_version.states.get(status__key="mitigated")
    draft = risk_version.states.get(status__key="draft")
    StateMachineTransition.objects.create(
        state_machine_version=risk_version,
        from_state=mitigated,
        to_state=draft,
        action_type=risk_version.transitions.first().action_type,
    )
    risk.status_key = "mitigated"
    risk.save(update_fields=["status_key"])
    with pytest.raises(TransitionNotAllowed, match="terminal"):
        transition(risk, "risk.create")


def test_a_failing_guard_blocks_the_move_and_leaves_the_record_alone(risk_version):
    risk = Risk.objects.create(title="Huge", amount=5000)
    transition(risk, "risk.assess")
    with pytest.raises(GuardFailed, match="obj.amount <= 1000"):
        transition(risk, "risk.mitigate")
    risk.refresh_from_db()
    assert risk.status_key == "assessed"
    assert StatusTransition.objects.for_object(risk).count() == 1


def test_a_passing_guard_lets_the_move_through(risk):
    transition(risk, "risk.assess")
    transition(risk, "risk.mitigate")
    assert risk.status_key == "mitigated"


def test_a_required_permission_is_enforced(risk, user):
    transition(risk, "risk.assess")
    with pytest.raises(PermissionDenied, match="testapp.change_risk"):
        transition(risk, "risk.reject", actor=user)


def test_a_user_holding_the_permission_may_proceed(risk, privileged_user):
    transition(risk, "risk.assess")
    transition(risk, "risk.reject", actor=privileged_user)
    assert risk.status_key == "rejected"


def test_permission_checks_can_be_bypassed_for_system_driven_moves(risk):
    transition(risk, "risk.assess")
    transition(risk, "risk.reject", enforce_permissions=False)
    assert risk.status_key == "rejected"


def test_an_approval_flagged_transition_refuses_to_commit_without_one(risk):
    with pytest.raises(ApprovalRequired, match="requires an approval"):
        transition(risk, "risk.discard")


def test_supplying_an_approval_commits_it_and_records_it(risk, user):
    record = transition(risk, "risk.discard", approval=user)
    assert risk.status_key == "rejected"
    assert record.metadata["approval"] == {"model": "auth.user", "pk": str(user.pk)}


def test_records_cannot_move_under_a_draft_version(risk_machine, risk):
    draft = clone_version(risk_machine.default_version, "2")
    risk.status_machine_version = draft
    risk.save(update_fields=["status_machine_version"])
    with pytest.raises(InvalidVersionState, match="is draft"):
        transition(risk, "risk.assess")


def test_a_draft_can_be_exercised_explicitly_while_authoring(risk_machine, risk):
    draft = clone_version(risk_machine.default_version, "2")
    risk.status_machine_version = draft
    risk.save(update_fields=["status_machine_version"])
    transition(risk, "risk.assess", allow_unpublished=True)
    assert risk.status_key == "assessed"


def test_history_recording_can_be_skipped(risk):
    assert transition(risk, "risk.assess", record_history=False) is None
    assert StatusTransition.objects.for_object(risk).count() == 0


def test_metadata_travels_into_the_history_row(risk):
    record = transition(risk, "risk.assess", metadata={"ticket": "SEC-12"})
    assert record.metadata == {"ticket": "SEC-12"}


def test_an_anonymous_actor_is_recorded_as_the_system(risk):
    from django.contrib.auth.models import AnonymousUser

    record = transition(risk, "risk.assess", actor=AnonymousUser())
    # A real identity row rather than a NULL, so every history row has an actor and the
    # browse index never has to reason about a missing one.
    assert record.actor.identity_type == IdentityType.SYSTEM
    assert record.actor.identity_key == ""
    assert record.actor.user_id is None
    assert record.actor_type == IdentityType.SYSTEM


def test_a_named_actor_is_snapshotted_rather_than_linked(risk, privileged_user):
    """The history keeps what the actor could do then, not what they can do now."""
    transition(risk, "risk.assess")
    record = transition(risk, "risk.reject", actor=privileged_user)

    assert record.actor.identity_type == IdentityType.USER
    assert record.actor.identity_key == str(privileged_user.pk)
    assert record.actor.identity_label == "bo"
    assert record.actor.user_id == privileged_user.pk
    assert "testapp.change_risk" in record.actor.permission_keys
    # Denormalised onto the row itself, which is what the browse index is built on.
    assert (record.actor_type, record.actor_key) == (IdentityType.USER, str(privileged_user.pk))

    # Taking the permission away does not rewrite what already happened.
    privileged_user.user_permissions.clear()
    record.refresh_from_db()
    assert "testapp.change_risk" in record.actor.permission_keys


def test_deleting_the_user_leaves_the_actor_legible(risk, user):
    record = transition(risk, "risk.assess", actor=user)
    label, key = record.actor.identity_label, record.actor.identity_key

    user.delete()

    record.refresh_from_db()
    assert record.actor.user_id is None
    assert (record.actor.identity_label, record.actor.identity_key) == (label, key)


def test_two_moves_by_one_person_are_two_snapshots(risk, user):
    """One row per reference, not per principal: the second move may differ."""
    first = transition(risk, "risk.assess", actor=user)
    second = transition(risk, "risk.mitigate", actor=user)

    assert first.actor_id != second.actor_id
    assert first.actor.identity_key == second.actor.identity_key


def test_passing_a_saved_identity_reuses_it(risk, user):
    """Two transitions can be tied to one snapshot by passing the row back in."""
    first = transition(risk, "risk.assess", actor=user)
    second = transition(risk, "risk.mitigate", actor=first.actor)

    assert second.actor_id == first.actor_id


def test_the_deprecated_user_kwarg_still_works(risk, user):
    with pytest.warns(DeprecationWarning, match="use actor="):
        # The signature no longer advertises it, which is the point of the shim.
        record = transition(risk, "risk.assess", user=user)  # type: ignore[call-arg]
    assert record.actor.identity_key == str(user.pk)


def test_passing_both_actor_and_user_is_an_error(risk, user):
    with pytest.raises(TypeError, match="Pass only 'actor'"):
        transition(risk, "risk.assess", actor=user, user=user)  # type: ignore[call-arg]


def test_publishing_a_new_version_leaves_existing_records_on_the_old_graph(risk_machine, risk):
    old_version_id = risk.status_machine_version_id
    draft = clone_version(risk_machine.default_version, "2")
    draft.states.filter(status__key="assessed").delete()
    draft.transitions.filter(to_state__isnull=True).delete()
    publish_version(draft, validate=False)

    risk.refresh_from_db()
    assert risk.status_machine_version_id == old_version_id
    transition(risk, "risk.assess")  # still legal under the pinned version
    assert risk.status_key == "assessed"


def test_new_records_pick_up_the_new_default_version(risk_machine, risk_version):
    draft = clone_version(risk_version, "2")
    publish_version(draft)
    fresh = Risk.objects.create(title="Later")
    assert fresh.status_machine_version_id == draft.pk
