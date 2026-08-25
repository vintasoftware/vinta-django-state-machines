"""Declaring, deconstructing and validating status fields."""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured, ValidationError

from tests.testapp.models import Risk, Roadmap, Unpinned
from vinta_state_machines.fields import (
    StatusKeyField,
    default_version_field_name,
    get_status_field_config,
    status_fields_of,
)
from vinta_state_machines.services import define_machine, publish_version


@pytest.mark.parametrize(
    ("field_name", "expected"),
    [
        ("status_key", "status_machine_version"),
        ("engagement_status_key", "engagement_status_machine_version"),
        ("state", "state_machine_version"),
        ("status", "status_machine_version"),
    ],
)
def test_the_companion_field_name_is_derived_from_the_status_field(field_name, expected):
    assert default_version_field_name(field_name) == expected


def test_the_companion_field_can_be_named_explicitly():
    config = get_status_field_config(Roadmap, "engagement_status_key")
    assert config.version_field == "engagement_machine_version"
    assert config.machine_key == "roadmap.engagement_status"


def test_a_model_can_carry_two_independently_governed_statuses():
    assert {field.name for field in status_fields_of(Roadmap)} == {
        "status_key",
        "engagement_status_key",
    }
    assert get_status_field_config(Roadmap, "status_key").machine_key == "roadmap.status"


def test_asking_for_a_field_that_is_not_governed_says_which_ones_are():
    with pytest.raises(ImproperlyConfigured, match="Status fields on this model: status_key"):
        get_status_field_config(Risk, "title")


def test_asking_for_a_field_that_does_not_exist_is_explained():
    with pytest.raises(ImproperlyConfigured, match="has no field 'nope'"):
        get_status_field_config(Risk, "nope")


def test_the_field_round_trips_through_deconstruct():
    field = StatusKeyField(machine="risk.status", version_field="custom", autopin=False)
    _name, path, _args, kwargs = field.deconstruct()
    assert path == "vinta_state_machines.fields.StatusKeyField"
    assert kwargs["machine"] == "risk.status"
    assert kwargs["version_field"] == "custom"
    assert kwargs["autopin"] is False


def test_defaults_are_left_out_of_the_deconstruction():
    _name, _path, _args, kwargs = StatusKeyField(machine="risk.status").deconstruct()
    assert "version_field" not in kwargs
    assert "autopin" not in kwargs


def test_the_version_field_protects_the_version_it_points_at():
    from django.db import models

    field = Risk._meta.get_field("status_machine_version")
    assert field.remote_field.on_delete is models.PROTECT
    assert field.null is True


@pytest.mark.django_db
def test_full_clean_rejects_a_status_the_pinned_version_does_not_declare(risk_version):
    risk = Risk(title="Odd", status_key="invented", status_machine_version=risk_version)
    with pytest.raises(ValidationError, match="is not a state of"):
        risk.full_clean()


@pytest.mark.django_db
def test_full_clean_accepts_a_status_the_pinned_version_declares(risk_version):
    Risk(title="Fine", status_key="assessed", status_machine_version=risk_version).full_clean()


@pytest.mark.django_db
def test_autopinning_is_skipped_when_the_machine_is_not_in_the_catalog_yet():
    """A brand new database must not make ``save()`` explode."""
    record = Unpinned.objects.create()
    assert record.status_key == ""


@pytest.mark.django_db
def test_autopinning_can_be_made_strict(settings):
    settings.STATE_MACHINES = {"STRICT": True, "CACHE_GRAPHS": False}
    with pytest.raises(LookupError, match="risk.status"):
        Risk.objects.create(title="No catalog")


@pytest.mark.django_db
def test_an_explicit_pin_is_never_overwritten(risk_version):
    other = define_machine(
        {
            "key": "risk.status.alt",
            "entity_type": "risk",
            "status_field": "alt_status",
            "name": "Alt",
            "version": "1",
            "states": [{"key": "start", "name": "Start", "is_initial": True}],
        }
    )
    publish_version(other, make_default=False)
    risk = Risk.objects.create(title="Pinned by hand", status_machine_version=other)
    assert risk.status_machine_version_id == other.pk
    assert risk.status_key == "start"


@pytest.mark.django_db
def test_the_mixin_mirrors_the_module_level_functions(risk):
    assert risk.current_state().key == "draft"
    assert risk.available_actions() == ["risk.assess"]
    assert risk.can_transition("risk.assess") is True
    risk.transition("risk.assess")
    assert risk.status_key == "assessed"
    assert risk.status_history().count() == 1
    assert risk.state_machine_graph().machine_key == "risk.status"
    assert risk.state_machine_version().version == "1"
