"""Django application definition."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

from vinta_state_machines.conf import install_swappable_defaults

# ``Meta.swappable`` resolves against *top level* Django settings, and every scope and
# actor foreign key in this app is declared against one of them, so both have to exist
# before the models are imported.  Django imports every app's ``apps`` module in the
# first phase of ``populate()`` -- before any model module -- which makes this the one
# place a default can land without asking every existing project to declare two new
# settings.  See the function's docstring for why ``ready()`` is too late.
install_swappable_defaults()


class StateMachinesConfig(AppConfig):
    name = "vinta_state_machines"
    label = "state_machines"
    verbose_name = _("State machines")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        # Importing these wires the cache invalidation signals, the model checks, and
        # the app's own side-effect handlers.
        from vinta_state_machines import batch_effects, checks, signals  # noqa: F401
        from vinta_state_machines.side_effects import autodiscover

        # Every installed app's ``side_effects`` module registers its handlers, so a
        # hook row can reference them by key from anywhere.
        autodiscover()
