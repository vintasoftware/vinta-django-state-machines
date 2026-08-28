"""The swap itself: a project pointing both model settings at its own tables.

Run under ``tests.settings_swapped``, because ``Meta.swappable`` is resolved once when
the models are imported and cannot be moved mid-process.
"""

from __future__ import annotations

import copy
import json

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction

from tests.swapped.models import Organization, OrganizationIdentity, OrganizationScope
from tests.testapp.models import Risk
from vinta_state_machines.engine import transition
from vinta_state_machines.enums import IdentityType, ScopeType
from vinta_state_machines.identities import get_identity_model
from vinta_state_machines.models import (
    StateMachine,
    StateMachineIdentity,
    StateMachineScope,
    StatusTransition,
)
from vinta_state_machines.scopes import get_default_scope, get_scope_model, scope_from_key
from vinta_state_machines.services import define_machine, publish_version
from vinta_state_machines.types import IdentitySnapshot

pytestmark = pytest.mark.django_db


@pytest.fixture
def acme_org(db) -> Organization:
    return Organization.objects.create(slug="o1", name="Acme")


@pytest.fixture
def acme(acme_org) -> OrganizationScope:
    """The scope row wrapping Acme -- the library's handle on the project's tenant."""
    scope = OrganizationScope()
    scope.scope = acme_org
    scope.save()
    return scope


@pytest.fixture
def acme_version(acme, risk_version, risk_definition):
    definition = copy.deepcopy(risk_definition)
    definition["scope"] = acme.scope_key
    definition["transitions"] = [
        edge for edge in definition["transitions"] if edge["name"] != "discard"
    ]
    version = define_machine(definition)
    publish_version(version)
    version.refresh_from_db()
    return version


# ---------------------------------------------------------------------- the swap


def test_both_models_are_the_projects_own():
    assert get_scope_model() is OrganizationScope
    assert get_identity_model() is OrganizationIdentity
    assert StateMachine._meta.get_field("scope").related_model is OrganizationScope
    assert StatusTransition._meta.get_field("scope").related_model is OrganizationScope
    assert StatusTransition._meta.get_field("actor").related_model is OrganizationIdentity
    assert StateMachine._meta.get_field("author").related_model is OrganizationIdentity


def test_the_shipped_tables_are_not_created_when_swapped_out():
    assert StateMachineScope._meta.swapped == "swapped.OrganizationScope"
    assert StateMachineIdentity._meta.swapped == "swapped.OrganizationIdentity"
    tables = connection.introspection.table_names()
    assert "state_machines_statemachinescope" not in tables
    assert "state_machines_statemachineidentity" not in tables


def test_the_swapped_models_satisfy_the_system_checks():
    from vinta_state_machines.checks import check_identity_model, check_scope_model

    assert check_scope_model() == []
    assert check_identity_model() == []


# ------------------------------------------------------------------ scope behaviour


def test_a_record_resolves_to_its_organizations_machine(acme_version, risk_version):
    risk = Risk.objects.create(title="Backups", owner_id=1)
    assert risk.status_machine_version_id == acme_version.pk


def test_a_record_outside_any_organization_uses_the_global_machine(acme_version, risk_version):
    risk = Risk.objects.create(title="Backups", owner_id=None)
    assert risk.status_machine_version_id == risk_version.pk


def test_an_organization_without_its_own_machine_falls_back(acme_version, risk_version):
    risk = Risk.objects.create(title="Backups", owner_id=99)
    assert risk.status_machine_version_id == risk_version.pk


def test_history_reaches_the_organization_through_the_scope(acme_version, acme, acme_org):
    risk = Risk.objects.create(title="Backups", owner_id=1)
    transition(risk, "risk.assess")
    record = StatusTransition.objects.latest("created_at")

    assert record.scope == acme
    # A real foreign key to the project's own table, one hop away.
    assert record.scope.scope == acme_org
    assert isinstance(record.scope.organization, Organization)
    # And the project's own key scheme, denormalised onto the row.
    assert record.scope_key == "org:o1"


def test_the_global_scope_row_is_the_projects_model_too(risk_version):
    transition(Risk.objects.create(title="Backups"), "risk.assess")
    record = StatusTransition.objects.latest("created_at")

    assert isinstance(record.scope, OrganizationScope)
    assert record.scope == get_default_scope()
    assert record.scope.organization_id is None
    assert record.scope_key == ""


def test_a_scope_with_history_cannot_be_deleted(acme_version, acme):
    from django.db.models import ProtectedError

    transition(Risk.objects.create(title="Backups", owner_id=1), "risk.assess")
    with pytest.raises(ProtectedError):
        acme.delete()


def test_a_scope_with_machines_cannot_be_deleted(acme_version, acme):
    """PROTECT throughout: a tenant's machines and its history outlive the tenant row."""
    from django.db.models import ProtectedError

    with pytest.raises(ProtectedError):
        acme.delete()


def test_the_projects_own_check_constraint_holds(acme):
    """The invariant the abstract base asks every subclass to enforce in the database."""
    with pytest.raises(IntegrityError), transaction.atomic():
        OrganizationScope.objects.filter(pk=acme.pk).update(scope_type=ScopeType.GLOBAL)


def test_two_machines_for_one_organization_and_field_are_rejected(acme_version, acme):
    with pytest.raises(IntegrityError), transaction.atomic():
        StateMachine.objects.create(
            key="risk.other",
            entity_type="risk",
            status_field="status",
            name="Clash",
            scope=acme,
        )


# --------------------------------------------------------------- identity behaviour


def test_the_actor_is_written_to_the_projects_own_table(risk_version, user):
    record = transition(Risk.objects.create(title="Backups"), "risk.assess", actor=user)

    assert isinstance(record.actor, OrganizationIdentity)
    assert record.actor.identity_key == str(user.pk)
    assert record.actor_type == IdentityType.USER


def test_the_projects_extra_column_is_filled_from_the_snapshot(risk_version):
    """``from_snapshot`` is the hook, and the library never has to know about it."""
    snapshot = IdentitySnapshot(
        identity_type=IdentityType.SERVICE,
        identity_key="billing-worker",
        identity_label="Billing worker",
        metadata={"department": "finance", "region": "eu"},
    )
    record = transition(Risk.objects.create(title="Backups"), "risk.assess", actor=snapshot)

    assert record.actor.department == "finance"
    # What the model did not promote stays in metadata rather than being dropped.
    assert record.actor.metadata == {"region": "eu"}


def test_the_system_actor_lands_in_the_projects_table_too(risk_version):
    record = transition(Risk.objects.create(title="Backups"), "risk.assess")

    assert isinstance(record.actor, OrganizationIdentity)
    assert record.actor.identity_type == IdentityType.SYSTEM
    assert record.actor.department == ""


def test_the_author_of_a_version_is_the_projects_identity(risk_version, user):
    from vinta_state_machines.services import clone_version

    draft = clone_version(risk_version, "2", author=user)

    assert isinstance(draft.author, OrganizationIdentity)
    assert draft.author.identity_key == str(user.pk)


# --------------------------------------------------------------------- portability


def test_scope_from_key_uses_the_projects_own_key_scheme(acme):
    assert scope_from_key("org:o1") == acme
    with pytest.raises(LookupError):
        scope_from_key("org:nope")


def test_export_and_import_round_trip_through_the_projects_key(acme_version, acme, tmp_path):
    out = tmp_path / "acme.json"
    call_command(
        "export_state_machine", "risk.status", "--scope", acme.scope_key, "--output", str(out)
    )
    payload = json.loads(out.read_text())
    assert payload["scope"] == "org:o1"

    payload["version"] = "2"
    again = tmp_path / "again.json"
    again.write_text(json.dumps(payload))
    call_command("import_state_machine", str(again))
    assert StateMachine.objects.get(key="risk.status", scope=acme).versions.count() == 2
