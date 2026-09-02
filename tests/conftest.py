"""Shared fixtures: a published risk machine and a couple of records on it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from django.contrib.auth.models import Permission, User

from tests.testapp import side_effects
from tests.testapp.models import Risk
from vinta_state_machines.graph import clear_graph_cache
from vinta_state_machines.models import StateMachine, StateMachineVersion
from vinta_state_machines.services import define_machine, publish_version

if TYPE_CHECKING:
    from collections.abc import Iterator

RISK_DEFINITION = {
    "key": "risk.status",
    "entity_type": "risk",
    "status_field": "status",
    "name": "Risk status",
    "version": "1",
    "states": [
        {"key": "draft", "name": "Draft", "is_initial": True, "order": 0, "x": 0, "y": 0},
        {"key": "assessed", "name": "Assessed", "order": 1, "x": 200, "y": 0},
        {
            "key": "mitigated",
            "name": "Mitigated",
            "is_terminal": True,
            "order": 2,
            "x": 400,
            "y": -80,
        },
        {
            "key": "rejected",
            "name": "Rejected",
            "is_terminal": True,
            "order": 3,
            "x": 400,
            "y": 80,
        },
    ],
    "transitions": [
        {"name": "create", "from": None, "to": "draft", "action": "risk.create"},
        {"name": "assess", "from": "draft", "to": "assessed", "action": "risk.assess"},
        {
            "name": "mitigate",
            "from": "assessed",
            "to": "mitigated",
            "action": "risk.mitigate",
            "guard": "obj.amount <= 1000",
        },
        {
            "name": "reject",
            "from": "assessed",
            "to": "rejected",
            "action": "risk.reject",
            "required_permission": "testapp.change_risk",
        },
        {
            "name": "discard",
            "from": "draft",
            "to": "rejected",
            "action": "risk.discard",
            "requires_approval": True,
        },
    ],
}


@pytest.fixture(autouse=True)
def _isolate_state() -> Iterator[None]:
    """Graphs and side-effect traces must not leak between tests."""
    clear_graph_cache()
    side_effects.reset()
    yield
    clear_graph_cache()
    side_effects.reset()


@pytest.fixture
def risk_definition() -> dict[str, Any]:
    """A fresh, mutable copy of the canonical definition."""
    import copy

    return copy.deepcopy(RISK_DEFINITION)


@pytest.fixture
def risk_draft(db, risk_definition) -> StateMachineVersion:
    return define_machine(risk_definition)


@pytest.fixture
def risk_version(risk_draft) -> StateMachineVersion:
    publish_version(risk_draft)
    risk_draft.refresh_from_db()
    return risk_draft


@pytest.fixture
def risk_machine(risk_version) -> StateMachine:
    return StateMachine.objects.get(key="risk.status")


@pytest.fixture
def risk(risk_version) -> Risk:
    """A saved record, autopinned to the published version and sitting on ``draft``."""
    return Risk.objects.create(title="Data retention", amount=500)


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(username="ana", password="x")


@pytest.fixture
def privileged_user(db) -> User:
    account = User.objects.create_user(username="bo", password="x")
    account.user_permissions.add(
        Permission.objects.get(content_type__app_label="testapp", codename="change_risk")
    )
    return User.objects.get(pk=account.pk)


@pytest.fixture
def import_run(db):
    """A parent record. Its machine is not defined here; these tests do not move it."""
    from tests.testapp.models import ImportRun

    return ImportRun.objects.create(label="nightly")
