"""Per tenant machines: resolution, the constraints that keep them honest, portability."""

from __future__ import annotations

import copy
import json

import pytest
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction
from django.test import override_settings

from tests.testapp.models import Risk
from vinta_state_machines.engine import resolve_version, transition
from vinta_state_machines.models import (
    StateMachine,
    StateMachineScope,
    StatusTransition,
)
from vinta_state_machines.scopes import resolve_machine, scope_from_key
from vinta_state_machines.services import define_machine, publish_version

pytestmark = pytest.mark.django_db

RESOLVER = "tests.testapp.tenancy.scope_for_owner"
KEY_RESOLVER = "tests.testapp.tenancy.scope_key_for_owner"


def tenant_scoped(*, enabled: bool = True, resolver: str = RESOLVER):
    """Turn tenancy on for one test without disturbing the other settings."""
    return override_settings(
        STATE_MACHINES={
            "CACHE_GRAPHS": False,
            "SCOPE_RESOLVER": resolver if enabled else None,
        }
    )


@pytest.fixture
def acme(db) -> StateMachineScope:
    return StateMachineScope.objects.create(key="org.1", name="Acme")


@pytest.fixture
def acme_version(acme, risk_version, risk_definition):
    """Acme's own risk machine: same vocabulary, one fewer way out of ``draft``."""
    definition = copy.deepcopy(risk_definition)
    definition["scope"] = acme.key
    definition["transitions"] = [
        edge for edge in definition["transitions"] if edge["name"] != "discard"
    ]
    version = define_machine(definition)
    publish_version(version)
    version.refresh_from_db()
    return version


# ------------------------------------------------------------------- resolution


def test_without_a_resolver_every_record_uses_the_global_machine(acme_version, risk_version):
    risk = Risk.objects.create(title="Backups", owner_id=1)
    assert risk.status_machine_version_id == risk_version.pk


def test_a_tenants_own_machine_wins_over_the_global_one(acme_version, risk_version):
    with tenant_scoped():
        risk = Risk.objects.create(title="Backups", owner_id=1)
    assert risk.status_machine_version_id == acme_version.pk


def test_a_tenant_without_its_own_machine_falls_back_to_the_global_one(acme_version, risk_version):
    with tenant_scoped():
        risk = Risk.objects.create(title="Backups", owner_id=999)
    assert risk.status_machine_version_id == risk_version.pk


def test_an_unowned_record_falls_back_to_the_global_machine(acme_version, risk_version):
    with tenant_scoped():
        risk = Risk.objects.create(title="Backups", owner_id=None)
    assert risk.status_machine_version_id == risk_version.pk


def test_a_resolver_may_return_a_portable_key_instead_of_a_row(acme_version, risk_version):
    with tenant_scoped(resolver=KEY_RESOLVER):
        risk = Risk.objects.create(title="Backups", owner_id=1)
    assert risk.status_machine_version_id == acme_version.pk


def test_the_tenants_graph_is_the_one_that_governs(acme_version, risk_version):
    """Acme dropped ``discard``, so the same record has fewer moves under its machine."""
    with tenant_scoped():
        risk = Risk.objects.create(title="Backups", owner_id=1)
        other = Risk.objects.create(title="Backups", owner_id=999)
    # include_blocked, so this compares the graphs rather than what one caller may do.
    assert {edge.name for edge in risk.available_transitions(include_blocked=True)} == {"assess"}
    assert {edge.name for edge in other.available_transitions(include_blocked=True)} == {
        "assess",
        "discard",
    }


def test_an_unpinned_record_resolves_through_its_tenant(acme_version, risk_version):
    with tenant_scoped():
        risk = Risk(title="Unsaved", owner_id=1)
        assert resolve_version(risk).pk == acme_version.pk


def test_resolve_machine_prefers_the_scope_then_the_global(acme_version, risk_version):
    from vinta_state_machines.fields import get_status_field_config

    config = get_status_field_config(Risk, "status_key")
    with tenant_scoped():
        assert resolve_machine(config, Risk(owner_id=1)).scope.key == "org.1"
        assert resolve_machine(config, Risk(owner_id=999)).scope_id is None


# ---------------------------------------------------------------------- history


def test_a_history_row_is_stamped_with_the_machines_scope(acme_version, risk_version, acme):
    with tenant_scoped():
        risk = Risk.objects.create(title="Backups", owner_id=1)
        transition(risk, "risk.assess")
    record = StatusTransition.objects.latest("created_at")
    assert record.scope_id == acme.pk


def test_a_global_machines_history_has_no_scope(risk_version):
    risk = Risk.objects.create(title="Backups")
    transition(risk, "risk.assess")
    assert StatusTransition.objects.latest("created_at").scope_id is None


def test_a_tenants_history_is_one_filter_away(acme_version, risk_version, acme):
    with tenant_scoped():
        transition(Risk.objects.create(title="A", owner_id=1), "risk.assess")
        transition(Risk.objects.create(title="B", owner_id=999), "risk.assess")
    assert StatusTransition.objects.filter(scope=acme).count() == 1
    assert StatusTransition.objects.filter(scope__isnull=True).count() == 1


def test_a_scope_cannot_be_deleted_out_from_under_its_history(acme_version, acme, risk_version):
    with tenant_scoped():
        transition(Risk.objects.create(title="A", owner_id=1), "risk.assess")
    from django.db.models import ProtectedError

    with pytest.raises(ProtectedError):
        acme.delete()


# ------------------------------------------------------------------ constraints


def test_two_global_machines_for_one_entity_and_field_are_rejected(risk_version):
    with pytest.raises(IntegrityError), transaction.atomic():
        StateMachine.objects.create(
            key="risk.other", entity_type="risk", status_field="status", name="Clash"
        )


def test_two_machines_for_the_same_tenant_and_field_are_rejected(acme_version, acme):
    with pytest.raises(IntegrityError), transaction.atomic():
        StateMachine.objects.create(
            key="risk.other",
            entity_type="risk",
            status_field="status",
            name="Clash",
            scope=acme,
        )


def test_the_same_key_may_exist_once_per_scope(acme_version, risk_version, acme):
    assert StateMachine.objects.filter(key="risk.status").count() == 2
    with pytest.raises(IntegrityError), transaction.atomic():
        StateMachine.objects.create(
            key="risk.status", entity_type="other", status_field="status", name="Dup", scope=acme
        )


def test_a_second_global_machine_with_the_same_key_is_rejected(risk_version):
    with pytest.raises(IntegrityError), transaction.atomic():
        StateMachine.objects.create(
            key="risk.status", entity_type="other", status_field="status", name="Dup"
        )


# ----------------------------------------------------------------- portability


def test_scope_from_key_refuses_a_key_that_names_nothing(db):
    with pytest.raises(LookupError, match="no-such-tenant"):
        scope_from_key("no-such-tenant")


def test_scope_from_key_passes_none_through(db):
    assert scope_from_key(None) is None


def test_export_writes_the_scope_key_and_import_reads_it_back(acme_version, acme, tmp_path):
    out = tmp_path / "acme.json"
    call_command("export_state_machine", "risk.status", "--scope", acme.key, "--output", str(out))
    payload = json.loads(out.read_text())
    assert payload["scope"] == "org.1"

    payload["version"] = "2"
    reimported = tmp_path / "again.json"
    reimported.write_text(json.dumps(payload))
    call_command("import_state_machine", str(reimported))
    assert StateMachine.objects.get(key="risk.status", scope=acme).versions.count() == 2


def test_a_global_export_carries_no_scope_key(risk_version, tmp_path):
    out = tmp_path / "global.json"
    call_command("export_state_machine", "risk.status", "--output", str(out))
    assert "scope" not in json.loads(out.read_text())


def test_exporting_without_a_scope_finds_the_global_machine(acme_version, risk_version, tmp_path):
    out = tmp_path / "global.json"
    call_command("export_state_machine", "risk.status", "--output", str(out))
    assert "scope" not in json.loads(out.read_text())


def test_exporting_an_unknown_scope_is_refused(risk_version):
    with pytest.raises(CommandError, match="scope key"):
        call_command("export_state_machine", "risk.status", "--scope", "org.nope")


def test_importing_into_an_unknown_scope_is_refused(db, risk_definition):
    definition = copy.deepcopy(risk_definition)
    definition["scope"] = "org.nope"
    with pytest.raises(LookupError, match="org.nope"):
        define_machine(definition)


# ----------------------------------------------------------------- scope model


def test_the_default_scope_model_round_trips_its_key(acme):
    assert acme.scope_key == "org.1"
    assert StateMachineScope.from_scope_key("org.1") == acme
    assert StateMachineScope.from_scope_key("nope") is None


def test_a_scope_model_missing_the_contract_is_reported(monkeypatch):
    """A swapped in model that cannot round trip a key breaks export quietly."""
    from vinta_state_machines import scopes
    from vinta_state_machines.checks import check_scope_model

    class Bare:
        class _meta:  # noqa: N801
            label = "myapp.Tenant"

    monkeypatch.setattr(scopes, "get_scope_model", lambda: Bare)
    ids = {issue.id for issue in check_scope_model()}
    assert ids == {"state_machines.E005", "state_machines.E006"}


def test_a_scope_model_that_honours_the_contract_passes(monkeypatch):
    from vinta_state_machines.checks import check_scope_model

    assert check_scope_model() == []
