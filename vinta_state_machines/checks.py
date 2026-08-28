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

    Subclassing :class:`~vinta_state_machines.models.AbstractStateMachineScope` is the
    whole contract: it brings the two columns the round trip needs, the invariant that
    keeps them honest, and the ``build_scope_key`` hook a project fills in.
    """
    from vinta_state_machines.models import AbstractStateMachineScope
    from vinta_state_machines.scopes import get_scope_model

    return _check_swappable(
        model=get_scope_model(),
        base=AbstractStateMachineScope,
        setting="STATE_MACHINES_SCOPE_MODEL",
        error_id="state_machines.E005",
        hook="build_scope_key",
        hook_error_id="state_machines.E006",
        hook_hint=(
            "Return a stable string that identifies this scope, and '' for the global "
            "scope:\n"
            "    def build_scope_key(self) -> str:\n"
            "        return '' if self.org_id is None else f'org:{self.org.slug}'"
        ),
    )


@register("models")
def check_identity_model(app_configs: Any = None, **kwargs: Any) -> list[Any]:
    """A swapped in identity model has to be constructible from a snapshot.

    Every actor this app records arrives as an
    :class:`~vinta_state_machines.types.IdentitySnapshot` and is turned into a row by
    the model's own ``from_snapshot``.  A replacement that does not inherit that
    machinery fails at the first transition rather than at check time.
    """
    from vinta_state_machines.identities import get_identity_model
    from vinta_state_machines.models import AbstractStateMachineIdentity

    return _check_swappable(
        model=get_identity_model(),
        base=AbstractStateMachineIdentity,
        setting="STATE_MACHINES_IDENTITY_MODEL",
        error_id="state_machines.E007",
        hook="from_snapshot",
        hook_error_id="state_machines.E008",
        hook_hint=(
            "Inherit it from AbstractStateMachineIdentity, or override it to fill your "
            "own columns:\n"
            "    @classmethod\n"
            "    def from_snapshot(cls, snapshot): ..."
        ),
    )


def _check_swappable(
    *,
    model: type[models.Model],
    base: type[models.Model],
    setting: str,
    error_id: str,
    hook: str,
    hook_error_id: str,
    hook_hint: str,
) -> list[Any]:
    """The shared body of the two checks above: right base class, usable hook."""
    label = model._meta.label
    if not issubclass(model, base):
        return [
            Error(
                f"{label} is the configured {setting} but does not subclass {base.__name__}.",
                hint=(
                    f"Subclass it:\n"
                    f"    from vinta_state_machines.models import {base.__name__}\n"
                    f"    class {model.__name__}({base.__name__}): ..."
                ),
                obj=model,
                id=error_id,
            )
        ]
    # Inherited from the abstract base, the hook raises NotImplementedError rather than
    # being absent, so presence is not the question -- whether it was overridden is.
    if getattr(model, hook, None) is getattr(base, hook, None) and _is_abstract_hook(base, hook):
        return [
            Error(
                f"{label} is the configured {setting} but does not implement {hook!r}.",
                hint=hook_hint,
                obj=model,
                id=hook_error_id,
            )
        ]
    return []


def _is_abstract_hook(base: type[models.Model], hook: str) -> bool:
    """Whether the base's version of ``hook`` is the one that just raises."""
    func = getattr(base, hook, None)
    func = getattr(func, "__func__", func)
    code = getattr(func, "__code__", None)
    return code is not None and "NotImplementedError" in (code.co_names or ())
