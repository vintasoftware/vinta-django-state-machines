"""Settings access for the app.

Everything lives under a single ``STATE_MACHINES`` dict so that a project only ever
declares one setting::

    STATE_MACHINES = {
        "AUTOPIN_DEFAULT_VERSION": True,
        "STRICT": False,
        "PERMISSION_CHECKER": "myproject.perms.can_transition",
    }

The *model* settings are the exception. ``Meta.swappable`` resolves against a top level
name, so they are declared alongside ``AUTH_USER_MODEL`` rather than inside the dict::

    STATE_MACHINES_SCOPE_MODEL = "organizations.OrganizationScope"
    STATE_MACHINES_IDENTITY_MODEL = "accounts.PrincipalIdentity"
    STATE_MACHINES_BATCH_MODEL = "imports.ImportBatch"
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.signals import setting_changed
from django.dispatch import receiver
from django.utils.module_loading import import_string

SETTINGS_KEY = "STATE_MACHINES"

# The scope and identity models are swappable, and ``Meta.swappable`` resolves against a
# *top level* setting rather than a key inside ``STATE_MACHINES``.
# :func:`install_swappable_defaults` gives both a default before the models are imported.
SCOPE_MODEL_SETTING = "STATE_MACHINES_SCOPE_MODEL"
DEFAULT_SCOPE_MODEL = "state_machines.StateMachineScope"

IDENTITY_MODEL_SETTING = "STATE_MACHINES_IDENTITY_MODEL"
DEFAULT_IDENTITY_MODEL = "state_machines.StateMachineIdentity"

BATCH_MODEL_SETTING = "STATE_MACHINES_BATCH_MODEL"
DEFAULT_BATCH_MODEL = "state_machines.StatusBatch"

SWAPPABLE_DEFAULTS: tuple[tuple[str, str], ...] = (
    (SCOPE_MODEL_SETTING, DEFAULT_SCOPE_MODEL),
    (IDENTITY_MODEL_SETTING, DEFAULT_IDENTITY_MODEL),
    (BATCH_MODEL_SETTING, DEFAULT_BATCH_MODEL),
)


def install_swappable_defaults() -> None:
    """Give the two swappable model settings a default, if the project has not.

    ``Meta.swappable`` is not an ordinary setting lookup. Django's migration
    autodetector reads the named setting with a bare ``getattr(settings, name)`` and
    lets the ``AttributeError`` escape, which is why ``AUTH_USER_MODEL`` -- the pattern
    this follows -- is declared in ``django.conf.global_settings``. A third party app
    cannot add to that module, so the default has to be put into the project's settings.

    Timing is what makes this work, and why it is called at import time in ``apps.py``
    rather than from ``AppConfig.ready``. ``apps.populate`` runs in two phases: it builds
    every ``AppConfig`` first (importing each app module and its ``apps`` module), and
    only then imports any models. So this has already run by the time a field definition
    or the autodetector asks for either setting; ``ready()`` would be far too late.

    A project that *has* set either one is left alone -- these are defaults, not
    overrides.
    """
    for name, default in SWAPPABLE_DEFAULTS:
        if not hasattr(settings, name):
            setattr(settings, name, default)


def scope_model_path() -> str:
    """Dotted ``app_label.ModelName`` of the model every scope foreign key points at."""
    value: str = getattr(settings, SCOPE_MODEL_SETTING, DEFAULT_SCOPE_MODEL)
    return value


def identity_model_path() -> str:
    """Dotted ``app_label.ModelName`` of the model every actor foreign key points at."""
    value: str = getattr(settings, IDENTITY_MODEL_SETTING, DEFAULT_IDENTITY_MODEL)
    return value


def batch_model_path() -> str:
    """Dotted ``app_label.ModelName`` of the model a fan-out batch is recorded on."""
    value: str = getattr(settings, BATCH_MODEL_SETTING, DEFAULT_BATCH_MODEL)
    return value


DEFAULTS: dict[str, Any] = {
    # Pin ``state_machine.default_version`` on newly created rows and fill the status
    # field from the version's initial state when it was left blank.
    "AUTOPIN_DEFAULT_VERSION": True,
    # Raise instead of silently skipping autopin when the machine or its default
    # version cannot be resolved (missing catalog rows, unmigrated database, ...).
    "STRICT": False,
    # Dotted path to ``checker(user, permission, instance) -> bool``.
    "PERMISSION_CHECKER": None,
    # Dotted path to ``resolver(instance, config) -> scope | pk | scope_key | None``,
    # which decides whose machine governs a record.  ``None`` disables tenancy: every
    # record resolves to the global machine.
    "SCOPE_RESOLVER": None,
    # Dotted path to ``resolver(actor) -> IdentitySnapshot``, which decides how an
    # acting principal is snapshotted onto the rows that record it.  ``None`` uses
    # ``vinta_state_machines.identities.identity_from_actor``, which understands a
    # Django user, an identity row, an existing snapshot, and ``None``.
    "IDENTITY_RESOLVER": None,
    # Allow ``guard`` expressions to be evaluated.  Disable to only permit guards
    # registered by name through ``@register_guard``.
    "ALLOW_GUARD_EXPRESSIONS": True,
    # Cap on the size of a guard expression, in characters.
    "MAX_GUARD_EXPRESSION_LENGTH": 1000,
    # Write a ``StatusTransition`` row for every successful transition.
    "RECORD_HISTORY": True,
    # Capture the actor's groups and permissions onto the identity recorded with each
    # move.  Costs up to two extra queries per transition -- often one, since a
    # transition guarded by a permission has already warmed the backend's cache.  Turn
    # off to keep only the columns that identify the actor.
    "CAPTURE_AUTHORIZATION_SNAPSHOT": True,
    # Keep parsed version graphs in memory. Turn off in tests that recycle pks.
    "CACHE_GRAPHS": True,
    # Lifecycle values a version must have for records to transition under it.
    "TRANSITIONABLE_LIFECYCLES": ("published",),
    # Dotted path to ``dispatch(operation, batch_id) -> None``, which decides where a
    # batch's join and cancel cascade actually run.  ``None`` runs them inline, in
    # ``transaction.on_commit``: correct, synchronous in tests, and fine for a small
    # fan-out.  A project with a queue points this at a function that enqueues.
    "BATCH_DISPATCHER": None,
    # How deep batches may nest before ``open_batch`` refuses.  A machine whose child
    # machine is (transitively) itself would otherwise recurse without end.
    "MAX_BATCH_DEPTH": 10,
    # How long a batch may sit in ``joining`` before the sweeper assumes the worker
    # died and dispatches the join again.  A batch may override this per row.
    "BATCH_JOIN_RETRY_AFTER": timedelta(minutes=5),
    # How much side-effect activity is written to ``SideEffectRun``: ``"none"``,
    # ``"failures"`` or ``"all"``.  Timing every handler is free; storing a row per
    # handler per transition is not, so the default keeps the ones that explain a
    # failure and drops the rest.  One hook may override this through its
    # ``record_runs`` column.
    "RECORD_SIDE_EFFECT_RUNS": "failures",
    # Copy the exception's *message* onto a failed run, not just its class.  Off by
    # default: an exception string routinely quotes the value that broke it, and a
    # run row is a second place that value would then live.
    "CAPTURE_SIDE_EFFECT_ERROR_DETAIL": False,
    # Cap on a captured error message, in characters.
    "MAX_SIDE_EFFECT_ERROR_DETAIL": 500,
    # Dotted path to ``sink(runs) -> None``, taking a list of unsaved ``SideEffectRun``
    # instances.  ``None`` writes them with the ORM.  A project whose callers wrap
    # ``transition()`` in a transaction of their own points this somewhere durable:
    # a rollback out there takes the ORM-written rows with it.
    "SIDE_EFFECT_RUN_SINK": None,
    # Enforce each scope's ``ScopeCapabilityRule`` rows while authoring.  Turning this
    # off leaves the rules in place and stops consulting them, which is the switch to
    # reach for if a policy locks somebody out of their own editor.
    "ENFORCE_CAPABILITY_POLICY": True,
    # Permission that lets an actor wire up a key their scope's policy forbids.
    # Superusers hold it implicitly, as they do every permission.
    "CAPABILITY_BYPASS_PERMISSION": "state_machines.bypass_capability_policy",
}

_IMPORT_STRINGS = frozenset(
    {
        "PERMISSION_CHECKER",
        "SCOPE_RESOLVER",
        "IDENTITY_RESOLVER",
        "BATCH_DISPATCHER",
        "SIDE_EFFECT_RUN_SINK",
    }
)

_cache: dict[str, Any] = {}


def get_setting(name: str) -> Any:
    """Return one app setting, honouring the project's ``STATE_MACHINES`` overrides."""
    if name in _cache:
        return _cache[name]
    if name not in DEFAULTS:
        raise KeyError(f"{name!r} is not a django-state-machines setting")
    overrides = getattr(settings, SETTINGS_KEY, None) or {}
    value = overrides.get(name, DEFAULTS[name])
    if name in _IMPORT_STRINGS and isinstance(value, str):
        value = import_string(value)
    _cache[name] = value
    return value


@receiver(setting_changed)
def _reset_cache(*, setting: str, **kwargs: Any) -> None:
    if setting == SETTINGS_KEY:
        _cache.clear()
