"""Tenancy: which slice of the world a machine governs.

A :class:`~vinta_state_machines.models.AbstractStateMachineScope` is a bucket -- an
organization, a workspace -- that a :class:`~vinta_state_machines.models.StateMachine`
may be specific to.  One of those buckets is the *global* scope, and a machine in it
governs every tenant that has not been given one of its own, which is why a single
tenant project never has to think about this module beyond the single row in that table.

Resolution is deliberately a two step fallback: the record's own tenant first, the
global scope second.  A tenant customises one entity's flow without having to copy every
other machine it did not want to change.

The global scope being a *row* rather than a NULL is what keeps that fallback one code
path and lets ``StateMachine`` carry one unique constraint per rule instead of a pair --
a NULL does not compare equal to itself, so it slips past a plain unique index.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.apps import apps
from django.db import models

from vinta_state_machines.conf import get_setting, scope_model_path
from vinta_state_machines.enums import ScopeType

if TYPE_CHECKING:
    from vinta_state_machines.fields import StatusFieldConfig
    from vinta_state_machines.models import StateMachine

__all__ = [
    "get_default_scope",
    "get_scope_model",
    "resolve_machine",
    "resolve_scope_pk",
    "scope_from_key",
]


def get_scope_model() -> Any:
    """The concrete model ``STATE_MACHINES_SCOPE_MODEL`` points at."""
    return apps.get_model(scope_model_path(), require_ready=False)


def get_default_scope() -> Any:
    """The global scope row, creating it on first use.

    Created lazily rather than in a migration because the scope model is swappable: a
    project's own subclass may not exist yet when this app's migrations run, and a data
    migration in this app cannot reach into a table it does not own.  Lazily means the
    row appears the first time anything needs it and is found by an indexed lookup on
    every call after that.
    """
    model = get_scope_model()
    scope, _created = model.objects.get_or_create(
        scope_type=ScopeType.GLOBAL,
        scope_key="",
    )
    return scope


def scope_from_key(key: str | None) -> Any:
    """Resolve a portable :attr:`scope_key` to a row.

    ``None`` and ``""`` both mean the global scope, which is what an exported machine
    with no tenant carries.

    Raises rather than creating a tenant scope: a scope is a tenant, and importing a
    definition is not the moment to invent one.  The global scope is the exception --
    it belongs to the installation, not to anybody, so it is created on demand.
    """
    if not key:
        return get_default_scope()
    model = get_scope_model()
    scope = model.objects.filter(scope_type=ScopeType.SCOPED, scope_key=key).first()
    if scope is None:
        raise LookupError(
            f"No {model._meta.label} matches the scope key {key!r}. "
            "Create the scope before importing a machine into it."
        )
    return scope


def resolve_scope_pk(instance: models.Model | None, config: StatusFieldConfig) -> Any:
    """Which tenant governs ``instance``, as a primary key, or ``None`` for global.

    The configured resolver may return a scope instance, a primary key, or a
    :attr:`scope_key`.  Returning an instance or a pk costs no query; a key costs one
    lookup, which is why the first two are worth preferring on a hot path.
    """
    resolver = get_setting("SCOPE_RESOLVER")
    if resolver is None:
        return None
    scope = resolver(instance, config)
    if scope is None:
        return None
    if isinstance(scope, models.Model):
        return scope.pk
    if isinstance(scope, str):
        found = (
            get_scope_model().objects.filter(scope_type=ScopeType.SCOPED, scope_key=scope).first()
        )
        return None if found is None else found.pk
    return scope


def resolve_machine(
    config: StatusFieldConfig, instance: models.Model | None = None
) -> StateMachine | None:
    """The machine governing ``config`` for this record's tenant, or ``None``.

    Falls back to the global machine, so a tenant only needs rows for the flows it
    actually customises.
    """
    from vinta_state_machines.models import StateMachine

    queryset = StateMachine.objects.select_related("default_version__state_machine")
    scope_pk = resolve_scope_pk(instance, config)
    if scope_pk is not None:
        scoped = queryset.filter(key=config.machine_key, scope=scope_pk).first()
        if scoped is not None:
            return scoped
    return queryset.filter(key=config.machine_key, scope__scope_type=ScopeType.GLOBAL).first()
