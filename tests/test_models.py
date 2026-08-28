"""Catalog and history invariants that the database itself enforces."""

from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction

from vinta_state_machines.enums import Lifecycle
from vinta_state_machines.models import (
    ActionType,
    StateMachine,
    StateMachineHook,
    StateMachineVersion,
    StatusDefinition,
    StatusTransition,
)

pytestmark = pytest.mark.django_db


def test_status_definition_key_is_unique_per_entity_and_field():
    StatusDefinition.objects.create(
        entity_type="risk", status_field="status", key="open", name="Open"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        StatusDefinition.objects.create(
            entity_type="risk", status_field="status", key="open", name="Open again"
        )


def test_the_same_key_may_exist_for_a_different_status_field():
    StatusDefinition.objects.create(
        entity_type="risk", status_field="status", key="open", name="Open"
    )
    other = StatusDefinition.objects.create(
        entity_type="risk", status_field="review_status", key="open", name="Open"
    )
    assert str(other) == "risk.review_status:open"


def test_one_state_machine_per_entity_and_field(risk_machine):
    with pytest.raises(IntegrityError), transaction.atomic():
        StateMachine.objects.create(
            key="risk.status.duplicate", entity_type="risk", status_field="status", name="Dup"
        )


def test_version_labels_are_unique_within_a_machine(risk_machine, risk_version):
    with pytest.raises(IntegrityError), transaction.atomic():
        StateMachineVersion.objects.create(state_machine=risk_machine, version="1")


def test_a_draft_cannot_carry_a_published_at(risk_machine):
    from django.utils import timezone

    with pytest.raises(IntegrityError), transaction.atomic():
        StateMachineVersion.objects.create(
            state_machine=risk_machine,
            version="99",
            lifecycle=Lifecycle.DRAFT,
            published_at=timezone.now(),
        )


def test_a_hook_bound_to_a_transition_must_name_one(risk_version):
    with pytest.raises(IntegrityError), transaction.atomic():
        StateMachineHook.objects.create(
            state_machine_version=risk_version, handler_key="testapp.record", event="transition"
        )


def test_a_state_hook_must_not_also_name_a_transition(risk_version):
    edge = risk_version.transitions.first()
    with pytest.raises(IntegrityError), transaction.atomic():
        StateMachineHook.objects.create(
            state_machine_version=risk_version,
            handler_key="testapp.record",
            event="enter_state",
            transition=edge,
            state=risk_version.states.first(),
        )


def test_status_transitions_are_append_only(risk, user):
    from vinta_state_machines.engine import transition

    record = transition(risk, "risk.assess", actor=user)
    record.comment = "tampering"
    with pytest.raises(ValueError, match="append-only"):
        record.save()


def test_action_type_keys_are_globally_unique():
    ActionType.objects.create(key="risk.assess", name="Assess")
    with pytest.raises(IntegrityError), transaction.atomic():
        ActionType.objects.create(key="risk.assess", name="Assess again")


def test_history_queryset_filters_by_target(risk, user):
    from vinta_state_machines.engine import transition

    transition(risk, "risk.assess", actor=user)
    assert StatusTransition.objects.for_object(risk).count() == 1
    assert StatusTransition.objects.entering("assessed").count() == 1
    assert StatusTransition.objects.leaving("draft").count() == 1
