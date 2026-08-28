"""Settings for the swapped-model test run.

``Meta.swappable`` is resolved once, when the models are imported, so a project cannot
swap the scope or identity model halfway through a process.  That is why this lives in
its own settings module and its own pytest invocation rather than an
``override_settings``.

The point of the run is that the library works against models it has never seen:
``tests.swapped`` adds a real foreign key to one and an extra column to the other, and
the whole suite has to keep passing.
"""

from __future__ import annotations

from tests.settings import *  # noqa: F403

STATE_MACHINES_SCOPE_MODEL = "swapped.OrganizationScope"
STATE_MACHINES_IDENTITY_MODEL = "swapped.OrganizationIdentity"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "vinta_state_machines",
    "tests.testapp",
    "tests.swapped",
]

STATE_MACHINES = {
    "CACHE_GRAPHS": False,
    "SCOPE_RESOLVER": "tests.swapped.tenancy.scope_for_owner",
}
