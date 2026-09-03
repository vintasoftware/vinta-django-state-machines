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


# ------------------------------------------------------------------- fan-out

IMPORT_RUN_DEFINITION = {
    "key": "import_run.status",
    "entity_type": "import_run",
    "status_field": "status",
    "name": "Import run status",
    "version": "1",
    "states": [
        {"key": "pending", "name": "Pending", "is_initial": True, "order": 0},
        {"key": "processing", "name": "Processing", "order": 1},
        {"key": "completed", "name": "Completed", "is_terminal": True, "order": 2},
        {"key": "partially_failed", "name": "Partially failed", "is_terminal": True, "order": 3},
        {"key": "failed", "name": "Failed", "is_terminal": True, "order": 4},
        {"key": "timed_out", "name": "Timed out", "is_terminal": True, "order": 5},
        {"key": "cancelled", "name": "Cancelled", "is_terminal": True, "order": 6},
    ],
    "transitions": [
        {"name": "create", "from": None, "to": "pending", "action": "import_run.create"},
        {"name": "start", "from": "pending", "to": "processing", "action": "import_run.start"},
        # Four edges, one action. The engine walks them in order and takes the first
        # whose guard holds, which is how every ending routes through one join action.
        {
            "name": "finish_timed_out",
            "from": "processing",
            "to": "timed_out",
            "action": "import_run.finish",
            "guard": 'metadata["batch"]["failure_reason"] == "timeout"',
            "order": 0,
        },
        {
            "name": "finish_clean",
            "from": "processing",
            "to": "completed",
            "action": "import_run.finish",
            "guard": 'metadata["batch"]["failed"] == 0',
            "order": 1,
        },
        {
            "name": "finish_partial",
            "from": "processing",
            "to": "partially_failed",
            "action": "import_run.finish",
            "guard": 'metadata["batch"]["succeeded"] > 0',
            "order": 2,
        },
        {
            "name": "finish_failed",
            "from": "processing",
            "to": "failed",
            "action": "import_run.finish",
            "order": 3,
        },
        {
            "name": "cancel",
            "from": "processing",
            "to": "cancelled",
            "action": "import_run.cancel",
        },
    ],
}

IMPORT_ROW_DEFINITION = {
    "key": "import_row.status",
    "entity_type": "import_row",
    "status_field": "status",
    "name": "Import row status",
    "version": "1",
    "states": [
        {"key": "queued", "name": "Queued", "is_initial": True, "order": 0},
        # Deliberately *not* terminal: a processed row can be reopened, which is what
        # makes un-counting reachable at all.
        {"key": "processed", "name": "Processed", "order": 1},
        {"key": "rejected", "name": "Rejected", "is_terminal": True, "order": 2},
    ],
    "transitions": [
        {"name": "create", "from": None, "to": "queued", "action": "import_row.create"},
        {"name": "process", "from": "queued", "to": "processed", "action": "import_row.process"},
        {"name": "reject", "from": "queued", "to": "rejected", "action": "import_row.reject"},
        {"name": "reopen", "from": "processed", "to": "queued", "action": "import_row.reopen"},
    ],
}


@pytest.fixture
def run_version(db) -> StateMachineVersion:
    import copy

    version = define_machine(copy.deepcopy(IMPORT_RUN_DEFINITION))
    publish_version(version)
    version.refresh_from_db()
    return version


@pytest.fixture
def row_version(db) -> StateMachineVersion:
    import copy

    version = define_machine(copy.deepcopy(IMPORT_ROW_DEFINITION))
    publish_version(version)
    version.refresh_from_db()
    return version


@pytest.fixture
def waiting_run(run_version):
    """A parent record already sitting in ``processing``, ready to fan out."""
    from tests.testapp.models import ImportRun

    record = ImportRun.objects.create(label="nightly")
    record.transition("import_run.start")
    return record


@pytest.fixture
def row_draft(db) -> StateMachineVersion:
    """The child machine as an unpublished draft, for validation tests."""
    import copy

    return define_machine(copy.deepcopy(IMPORT_ROW_DEFINITION))


@pytest.fixture
def run_draft(db) -> StateMachineVersion:
    """The parent machine as an unpublished draft, for editor and validation tests."""
    import copy

    return define_machine(copy.deepcopy(IMPORT_RUN_DEFINITION))
