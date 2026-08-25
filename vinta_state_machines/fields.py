"""Model fields for status bearing records.

A status bearing model declares two fields per governed status: the soft reference to
the catalog, and the pin to the version it was created under::

    class Risk(StateMachineMixin, models.Model):
        status_key = StatusKeyField(machine="risk.status")
        status_machine_version = StateMachineVersionField()

Both are ordinary concrete fields, so migrations, ``select_related`` and typing all work
exactly as they would on a hand written pair.  Declaring the pair twice under different
names is how a model carries two independently governed statuses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import DatabaseError, models
from django.utils.translation import gettext_lazy as _

from vinta_state_machines.conf import get_setting

if TYPE_CHECKING:
    from vinta_state_machines.models import StateMachineVersion

    # Django's fields only accept type parameters under a type checker; at runtime they
    # are plain classes, so the generic aliases have to be checker-only.
    _CharField = models.CharField[str, str]
    _VersionForeignKey = models.ForeignKey[StateMachineVersion | None]
else:
    _CharField = models.CharField
    _VersionForeignKey = models.ForeignKey

__all__ = [
    "StateMachineMixin",
    "StateMachineVersionField",
    "StatusFieldConfig",
    "StatusKeyField",
    "get_status_field_config",
    "status_fields_of",
]

VERSION_SUFFIX = "_machine_version"


def default_version_field_name(field_name: str) -> str:
    """``status_key`` -> ``status_machine_version``; ``state`` -> ``state_machine_version``."""
    stem = field_name[: -len("_key")] if field_name.endswith("_key") else field_name
    return f"{stem}{VERSION_SUFFIX}"


class StatusKeyField(_CharField):
    """Stores the current status as a stable key from the catalog.

    It is a plain ``CharField`` on the database side — the point of the soft reference is
    that a status value travels across databases without a join — but it knows which
    :class:`~vinta_state_machines.models.StateMachine` governs it, which lets the engine
    resolve the graph from the record alone.
    """

    description = _("Current status, as a key from the status catalog")

    def __init__(
        self,
        *args: Any,
        machine: str,
        version_field: str | None = None,
        autopin: bool | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("max_length", 100)
        kwargs.setdefault("db_index", True)
        kwargs.setdefault("blank", True)
        kwargs.setdefault("default", "")
        self.machine_key = machine
        self.explicit_version_field = version_field
        self.autopin = autopin
        super().__init__(*args, **kwargs)

    def deconstruct(self) -> tuple[str, str, list[Any], dict[str, Any]]:
        name, path, args, kwargs = super().deconstruct()
        kwargs["machine"] = self.machine_key
        if self.explicit_version_field is not None:
            kwargs["version_field"] = self.explicit_version_field
        if self.autopin is not None:
            kwargs["autopin"] = self.autopin
        return name, path, list(args), kwargs

    @property
    def version_field(self) -> str:
        """Name of the companion field holding the pinned version."""
        return self.explicit_version_field or default_version_field_name(self.name)

    @property
    def autopin_enabled(self) -> bool:
        if self.autopin is not None:
            return self.autopin
        return bool(get_setting("AUTOPIN_DEFAULT_VERSION"))

    def config(self) -> StatusFieldConfig:
        return StatusFieldConfig(
            field_name=self.name,
            machine_key=self.machine_key,
            version_field=self.version_field,
            autopin=self.autopin_enabled,
        )

    # ------------------------------------------------------------------ runtime
    def pre_save(self, model_instance: models.Model, add: bool) -> Any:
        """On creation, pin the default version and fall back to the initial state.

        This is what makes ``Risk.objects.create(title=...)`` land on a correct, pinned
        status without every call site having to know the catalog.
        """
        value = super().pre_save(model_instance, add)
        if not add or not self.autopin_enabled:
            return value
        already_pinned = getattr(model_instance, f"{self.version_field}_id", None) is not None
        if already_pinned and value:
            return value
        try:
            version = (
                getattr(model_instance, self.version_field)
                if already_pinned
                else self._default_version(model_instance)
            )
        except (DatabaseError, LookupError) as exc:
            if get_setting("STRICT"):
                raise
            self._warn(exc)
            return value
        if version is None:
            return value
        if not already_pinned:
            setattr(model_instance, self.version_field, version)
        if not value:
            initial = version.graph().initial_states
            if initial:
                value = initial[0].key
                setattr(model_instance, self.attname, value)
        return value

    def _default_version(self, model_instance: models.Model) -> Any:
        """The default version of the machine governing *this record's* tenant.

        The instance is what makes the lookup tenant aware: the configured resolver
        reads the organization off the record being created, and falls back to the
        global machine when the tenant has not customised this flow.
        """
        from vinta_state_machines.scopes import resolve_machine

        machine = resolve_machine(self.config(), model_instance)
        if machine is None:
            raise LookupError(f"No StateMachine with key {self.machine_key!r}")
        return machine.default_version

    def _warn(self, exc: Exception) -> None:
        import logging

        logging.getLogger("vinta_state_machines").debug(
            "Could not autopin %s.%s: %s", self.model._meta.label, self.name, exc
        )

    # --------------------------------------------------------------- validation
    def validate(self, value: Any, model_instance: models.Model | None) -> None:
        """Reject a status that the pinned version does not declare as a state."""
        super().validate(value, model_instance)
        if not value or model_instance is None:
            return
        version = getattr(model_instance, self.version_field, None)
        if version is None:
            return
        graph = version.graph()
        if not graph.has_state(value):
            raise ValidationError(
                _("%(value)s is not a state of %(machine)s@%(version)s."),
                code="unknown_status",
                params={
                    "value": value,
                    "machine": graph.machine_key,
                    "version": graph.version_label,
                },
            )


class StateMachineVersionField(_VersionForeignKey):
    """The pin: which version's rules this record is validated against.

    ``on_delete`` is ``PROTECT`` and the default is ``null=True`` — a pinned version must
    outlive the records that pinned it, and a record may exist before the catalog does.
    """

    def __init__(self, to: Any = None, on_delete: Any = None, **kwargs: Any) -> None:
        kwargs.setdefault("null", True)
        kwargs.setdefault("blank", True)
        kwargs.setdefault("related_name", "+")
        kwargs.setdefault(
            "help_text",
            _("Pinned at creation; publishing a new version never migrates this row."),
        )
        super().__init__(
            to or "state_machines.StateMachineVersion",
            on_delete=on_delete or models.PROTECT,
            **kwargs,
        )

    def deconstruct(self) -> tuple[str, str, list[Any], dict[str, Any]]:
        name, path, args, kwargs = super().deconstruct()
        kwargs.pop("to", None)
        if kwargs.get("on_delete") is models.PROTECT:
            kwargs.pop("on_delete")
        return name, path, list(args), kwargs


# ---------------------------------------------------------------------- lookup


@dataclass(frozen=True)
class StatusFieldConfig:
    """What the engine needs to know about one governed status field."""

    field_name: str
    machine_key: str
    version_field: str
    autopin: bool


def get_status_field_config(model: type[models.Model], field_name: str) -> StatusFieldConfig:
    """Return the config of ``model.<field_name>``, or explain what is wrong."""
    try:
        field = model._meta.get_field(field_name)
    except Exception as exc:  # FieldDoesNotExist
        candidates = ", ".join(f.name for f in status_fields_of(model)) or "<none>"
        raise ImproperlyConfigured(
            f"{model._meta.label} has no field {field_name!r}. "
            f"Status fields on this model: {candidates}."
        ) from exc
    if not isinstance(field, StatusKeyField):
        candidates = ", ".join(f.name for f in status_fields_of(model)) or "<none>"
        raise ImproperlyConfigured(
            f"{model._meta.label}.{field_name} is a {type(field).__name__}, "
            f"not a StatusKeyField, so no state machine governs it. "
            f"Status fields on this model: {candidates}."
        )
    return field.config()


def status_fields_of(model: type[models.Model]) -> list[StatusKeyField]:
    """Every :class:`StatusKeyField` declared on ``model``."""
    return [f for f in model._meta.get_fields() if isinstance(f, StatusKeyField)]


class StateMachineMixin:
    """Convenience wrappers around :mod:`vinta_state_machines.engine`.

    Purely sugar: everything here is also available as a module level function taking the
    instance, which is what you want for models you do not control.
    """

    def state_machine_version(self, field_name: str = "status_key") -> Any:
        from vinta_state_machines.engine import resolve_version

        return resolve_version(self, field_name)  # type: ignore[arg-type]

    def state_machine_graph(self, field_name: str = "status_key") -> Any:
        from vinta_state_machines.engine import graph_for

        return graph_for(self, field_name)  # type: ignore[arg-type]

    def current_state(self, field_name: str = "status_key") -> Any:
        from vinta_state_machines.engine import current_state

        return current_state(self, field_name)  # type: ignore[arg-type]

    def available_transitions(self, field_name: str = "status_key", **kwargs: Any) -> Any:
        from vinta_state_machines.engine import available_transitions

        return available_transitions(self, field_name, **kwargs)  # type: ignore[arg-type]

    def available_actions(self, field_name: str = "status_key", **kwargs: Any) -> Any:
        from vinta_state_machines.engine import available_actions

        return available_actions(self, field_name, **kwargs)  # type: ignore[arg-type]

    def can_transition(self, action: str, field_name: str = "status_key", **kwargs: Any) -> bool:
        from vinta_state_machines.engine import can_transition

        return can_transition(self, action, field_name, **kwargs)  # type: ignore[arg-type]

    def transition(self, action: str, field_name: str = "status_key", **kwargs: Any) -> Any:
        from vinta_state_machines.engine import transition

        return transition(self, action, field_name, **kwargs)  # type: ignore[arg-type]

    def status_history(self, field_name: str = "status_key") -> Any:
        from vinta_state_machines.models import StatusTransition

        config = get_status_field_config(type(self), field_name)  # type: ignore[arg-type]
        del config
        return StatusTransition.objects.for_object(self).with_related()  # type: ignore[arg-type]
