"""Django application definition."""

from __future__ import annotations

from django.apps import AppConfig
from django.conf import settings
from django.utils.translation import gettext_lazy as _

from vinta_state_machines.conf import DEFAULT_SCOPE_MODEL, SCOPE_MODEL_SETTING

# ``Meta.swappable`` resolves against a *top level* Django setting, and both scope
# foreign keys are declared against this one, so it has to exist before the models are
# imported.  Django imports every app's ``apps`` module in the first phase of
# ``populate()`` -- before any model module -- which makes this the one place a default
# can land without asking every existing project to declare a new setting.
if not hasattr(settings, SCOPE_MODEL_SETTING):
    setattr(settings, SCOPE_MODEL_SETTING, DEFAULT_SCOPE_MODEL)


class StateMachinesConfig(AppConfig):
    name = "vinta_state_machines"
    label = "state_machines"
    verbose_name = _("State machines")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        # Importing these wires the cache invalidation signals and the model checks.
        from vinta_state_machines import checks, signals  # noqa: F401
        from vinta_state_machines.side_effects import autodiscover

        # Every installed app's ``side_effects`` module registers its handlers, so a
        # hook row can reference them by key from anywhere.
        autodiscover()
