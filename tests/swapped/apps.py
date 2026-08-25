"""A project that points the scope model at its own tenant table."""

from __future__ import annotations

from django.apps import AppConfig


class SwappedConfig(AppConfig):
    name = "tests.swapped"
    label = "swapped"
    default_auto_field = "django.db.models.BigAutoField"
