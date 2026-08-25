"""The swap itself: a project pointing the scope model at its own tenant table.

Run under ``tests.settings_swapped``, because ``Meta.swappable`` is resolved once when
the models are imported and cannot be moved mid-process.
"""

from __future__ import annotations

import copy
import json

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction

from tests.swapped.models import Organization
from tests.testapp.models import Risk
from vinta_state_machines.engine import transition
from vinta_state_machines.models import StateMachine, StateMachineScope, StatusTransition
from vinta_state_machines.scopes import get_scope_model, scope_from_key
from vinta_state_machines.services import define_machine, publish_version

pytestmark = pytest.mark.django_db


@pytest.fixture
def acme(db) -> Organization:
    return Organization.objects.create(slug="o1", name="Acme")


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


def test_the_scope_model_is_the_projects_own(acme):
    assert get_scope_model() is Organization
    assert StateMachine._meta.get_field("scope").related_model is Organization
    assert StatusTransition._meta.get_field("scope").related_model is Organization


def test_the_default_scope_table_is_not_created_when_swapped_out():
    assert StateMachineScope._meta.swapped == "swapped.Organization"
    assert "state_machines_statemachinescope" not in connection.introspection.table_names()


def test_the_swapped_model_satisfies_the_system_check():
    from vinta_state_machines.checks import check_scope_model

    assert check_scope_model() == []


# --------------------------------------------------------------------- behaviour


def test_a_record_resolves_to_its_organizations_machine(acme_version, risk_version):
    risk = Risk.objects.create(title="Backups", owner_id=1)
    assert risk.status_machine_version_id == acme_version.pk


def test_a_record_outside_any_organization_uses_the_global_machine(acme_version, risk_version):
    risk = Risk.objects.create(title="Backups", owner_id=None)
    assert risk.status_machine_version_id == risk_version.pk


def test_an_organization_without_its_own_machine_falls_back(acme_version, risk_version):
    risk = Risk.objects.create(title="Backups", owner_id=99)
    assert risk.status_machine_version_id == risk_version.pk


def test_history_is_stamped_with_a_real_organization_foreign_key(acme_version, acme):
    risk = Risk.objects.create(title="Backups", owner_id=1)
    transition(risk, "risk.assess")
    record = StatusTransition.objects.latest("created_at")
    assert record.scope == acme
    assert isinstance(record.scope, Organization)


def test_an_organization_with_history_cannot_be_deleted(acme_version, acme):
    from django.db.models import ProtectedError

    transition(Risk.objects.create(title="Backups", owner_id=1), "risk.assess")
    with pytest.raises(ProtectedError):
        acme.delete()


def test_deleting_an_organization_takes_its_machines_with_it(acme_version, acme):
    """CASCADE on the machine, PROTECT on the log: config is disposable, audit is not."""
    pk = acme.pk  # delete() clears it on the instance
    assert StateMachine.objects.filter(scope_id=pk).exists()
    acme.delete()
    assert not StateMachine.objects.filter(scope_id=pk).exists()
    assert StateMachine.objects.filter(scope__isnull=True).exists()


def test_two_machines_for_one_organization_and_field_are_rejected(acme_version, acme):
    with pytest.raises(IntegrityError), transaction.atomic():
        StateMachine.objects.create(
            key="risk.other",
            entity_type="risk",
            status_field="status",
            name="Clash",
            scope=acme,
        )


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
