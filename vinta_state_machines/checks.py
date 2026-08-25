"""System checks for status bearing models.

These catch the mistakes that would otherwise only surface the first time a record tries
to move: a status field whose companion pin is missing, or points somewhere else.
"""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.core.checks import Error, Warning as CheckWarning, register
from django.db import models

from vinta_state_machines.fields import StateMachineVersionField, status_fields_of

VERSION_MODEL = "state_machines.StateMachineVersion"


@register("models")
def check_status_fields(app_configs: Any = None, **kwargs: Any) -> list[Any]:
    """Every :class:`StatusKeyField` must have a usable companion version field."""
    issues: list[Any] = []
    models_to_check = (
        apps.get_models()
        if app_configs is None
        else [model for config in app_configs for model in config.get_models()]
    )
    for model in models_to_check:
        for field in status_fields_of(model):
            issues.extend(_check_field(model, field))
    return issues


def _check_field(model: type[models.Model], field: Any) -> list[Any]:
    label = f"{model._meta.label}.{field.name}"
    try:
        companion = model._meta.get_field(field.version_field)
    except Exception:
        return [
            Error(
                f"{label} has no companion version field {field.version_field!r}.",
                hint=(
                    "Declare it alongside the status field:\n"
                    f"    {field.version_field} = StateMachineVersionField()\n"
                    "or point the status field elsewhere with "
                    "StatusKeyField(machine=..., version_field=...)."
                ),
                obj=model,
                id="state_machines.E001",
            )
        ]
    if not isinstance(companion, models.ForeignKey):
        return [
            Error(
                f"{label}'s companion field {field.version_field!r} is not a ForeignKey.",
                hint=f"Use StateMachineVersionField(), a ForeignKey to {VERSION_MODEL}.",
                obj=model,
                id="state_machines.E002",
            )
        ]
    related = companion.remote_field.model
    related_label = related if isinstance(related, str) else related._meta.label
    if related_label.lower() != VERSION_MODEL.lower():
        return [
            Error(
                f"{label}'s companion field {field.version_field!r} points at "
                f"{related_label}, not {VERSION_MODEL}.",
                obj=model,
                id="state_machines.E003",
            )
        ]
    issues: list[Any] = []
    if not isinstance(
        companion, StateMachineVersionField
    ) and companion.remote_field.on_delete not in (
        models.PROTECT,
        models.DO_NOTHING,
    ):
        issues.append(
            CheckWarning(
                f"{label}'s companion field {field.version_field!r} does not use "
                "on_delete=PROTECT.",
                hint=(
                    "A pinned version must outlive the records that pinned it, otherwise "
                    "their history stops being reproducible."
                ),
                obj=model,
                id="state_machines.W001",
            )
        )
    if not field.machine_key:
        issues.append(
            Error(
                f"{label} declares an empty machine key.",
                obj=model,
                id="state_machines.E004",
            )
        )
    return issues


@register("models")
def check_scope_model(app_configs: Any = None, **kwargs: Any) -> list[Any]:
    """A swapped in scope model has to keep exported machines portable.

    Primary keys do not cross databases, so export writes a scope *key* and import reads
    one back.  A replacement model that cannot do that round trip breaks the commands
    quietly, at the moment someone tries to move a machine between environments -- so it
    is worth failing at ``manage.py check`` instead.
    """
    from vinta_state_machines.scopes import get_scope_model

    model = get_scope_model()
    label = model._meta.label
    issues: list[Any] = []
    if not hasattr(model, "scope_key"):
        issues.append(
            Error(
                f"{label} is the configured STATE_MACHINES_SCOPE_MODEL but has no 'scope_key'.",
                hint=(
                    "Add a 'scope_key' property returning a stable string, e.g.\n"
                    "    @property\n"
                    "    def scope_key(self) -> str:\n"
                    "        return f'org:{self.slug}'"
                ),
                obj=model,
                id="state_machines.E005",
            )
        )
    if not callable(getattr(model, "from_scope_key", None)):
        issues.append(
            Error(
                f"{label} is the configured STATE_MACHINES_SCOPE_MODEL but has no "
                "'from_scope_key' classmethod.",
                hint=(
                    "Add the inverse of 'scope_key', returning None when nothing matches:\n"
                    "    @classmethod\n"
                    "    def from_scope_key(cls, key): ..."
                ),
                obj=model,
                id="state_machines.E006",
            )
        )
    return issues
