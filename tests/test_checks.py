"""System checks catch a mis-declared status field before it reaches runtime."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import models
from django.test.utils import isolate_apps

from tests.testapp.models import Risk, Roadmap
from vinta_state_machines.checks import check_status_fields
from vinta_state_machines.fields import StateMachineVersionField, StatusKeyField


@dataclass
class FakeAppConfig:
    """Lets a check run against one throwaway model without touching the registry."""

    model: type[models.Model]

    def get_models(self):
        return [self.model]


def ids(model):
    return sorted(issue.id for issue in check_status_fields([FakeAppConfig(model)]))


def hints(model):
    return [issue.hint for issue in check_status_fields([FakeAppConfig(model)])]


def test_correctly_declared_models_raise_nothing():
    assert check_status_fields(None) == []
    assert check_status_fields([Risk._meta.app_config]) == []


@isolate_apps("tests.testapp")
def test_a_missing_companion_field_is_an_error():
    class MissingPin(models.Model):
        status_key = StatusKeyField(machine="risk.status")

        class Meta:
            app_label = "testapp"

    assert ids(MissingPin) == ["state_machines.E001"]
    assert "status_machine_version" in hints(MissingPin)[0]


@isolate_apps("tests.testapp")
def test_a_companion_field_that_is_not_a_foreign_key_is_an_error():
    class WrongType(models.Model):
        status_key = StatusKeyField(machine="risk.status")
        status_machine_version = models.CharField(max_length=10, blank=True)

        class Meta:
            app_label = "testapp"

    assert ids(WrongType) == ["state_machines.E002"]


@isolate_apps("tests.testapp")
def test_a_companion_field_pointing_at_the_wrong_model_is_an_error():
    class WrongTarget(models.Model):
        status_key = StatusKeyField(machine="risk.status")
        status_machine_version = models.ForeignKey(
            "testapp.Risk", null=True, on_delete=models.PROTECT, related_name="+"
        )

        class Meta:
            app_label = "testapp"

    assert ids(WrongTarget) == ["state_machines.E003"]


@isolate_apps("tests.testapp")
def test_a_hand_rolled_pin_without_protect_is_a_warning():
    class Cascading(models.Model):
        status_key = StatusKeyField(machine="risk.status")
        status_machine_version = models.ForeignKey(
            "state_machines.StateMachineVersion",
            null=True,
            on_delete=models.CASCADE,
            related_name="+",
        )

        class Meta:
            app_label = "testapp"

    assert ids(Cascading) == ["state_machines.W001"]


@isolate_apps("tests.testapp")
def test_an_empty_machine_key_is_an_error():
    class NoMachine(models.Model):
        status_key = StatusKeyField(machine="")
        status_machine_version = StateMachineVersionField()

        class Meta:
            app_label = "testapp"

    assert ids(NoMachine) == ["state_machines.E004"]


def test_both_status_fields_of_a_multi_status_model_are_checked():
    assert check_status_fields([Roadmap._meta.app_config]) == []
