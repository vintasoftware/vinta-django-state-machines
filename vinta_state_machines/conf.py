"""Settings access for the app.

Everything lives under a single ``STATE_MACHINES`` dict so that a project only ever
declares one setting::

    STATE_MACHINES = {
        "AUTOPIN_DEFAULT_VERSION": True,
        "STRICT": False,
        "PERMISSION_CHECKER": "myproject.perms.can_transition",
    }
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.signals import setting_changed
from django.dispatch import receiver
from django.utils.module_loading import import_string

SETTINGS_KEY = "STATE_MACHINES"

# The scope model is swappable, and ``Meta.swappable`` resolves against a *top level*
# setting rather than a key inside ``STATE_MACHINES``. ``apps.py`` gives it a default
# before the models are imported.
SCOPE_MODEL_SETTING = "STATE_MACHINES_SCOPE_MODEL"
DEFAULT_SCOPE_MODEL = "state_machines.StateMachineScope"


def scope_model_path() -> str:
    """Dotted ``app_label.ModelName`` of the model both scope foreign keys point at."""
    value: str = getattr(settings, SCOPE_MODEL_SETTING, DEFAULT_SCOPE_MODEL)
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
    # Allow ``guard`` expressions to be evaluated.  Disable to only permit guards
    # registered by name through ``@register_guard``.
    "ALLOW_GUARD_EXPRESSIONS": True,
    # Cap on the size of a guard expression, in characters.
    "MAX_GUARD_EXPRESSION_LENGTH": 1000,
    # Write a ``StatusTransition`` row for every successful transition.
    "RECORD_HISTORY": True,
    # Keep parsed version graphs in memory. Turn off in tests that recycle pks.
    "CACHE_GRAPHS": True,
    # Lifecycle values a version must have for records to transition under it.
    "TRANSITIONABLE_LIFECYCLES": ("published",),
}

_IMPORT_STRINGS = frozenset({"PERMISSION_CHECKER", "SCOPE_RESOLVER"})

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
