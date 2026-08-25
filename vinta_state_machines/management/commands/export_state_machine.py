"""Dump a version back out as the JSON that ``import_state_machine`` reads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from django.core.management.base import BaseCommand, CommandError

from vinta_state_machines.models import (
    StateMachine,
    StateMachineHook,
    StateMachineVersion,
)
from vinta_state_machines.scopes import scope_from_key

if TYPE_CHECKING:
    from argparse import ArgumentParser


class Command(BaseCommand):
    help = "Export a state machine version as JSON."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("machine", help="State machine key, e.g. 'risk.status'.")
        parser.add_argument(
            "--scope",
            help="Scope key of a tenant specific machine. Omit for the global one.",
        )
        parser.add_argument(
            "--label",
            help="Version label, e.g. '2'. Defaults to the machine's default version.",
        )
        parser.add_argument("--output", type=Path, help="Write here instead of stdout.")
        parser.add_argument("--indent", type=int, default=2)

    def handle(self, *args: Any, **options: Any) -> None:
        machine = self._machine(options["machine"], options.get("scope"))

        version = self._resolve(machine, options.get("label"))
        payload = _serialize(machine, version)
        text = json.dumps(payload, indent=options["indent"], sort_keys=False)
        if options.get("output"):
            options["output"].write_text(text + "\n")
            self.stdout.write(self.style.SUCCESS(f"Wrote {options['output']}"))
        else:
            self.stdout.write(text)

    def _machine(self, key: str, scope_key: str | None) -> StateMachine:
        """The machine ``key`` names within ``scope_key``.

        A key is only unique per scope now, so the scope is part of the address rather
        than an optional filter.
        """
        try:
            scope = scope_from_key(scope_key)
        except LookupError as exc:
            raise CommandError(str(exc)) from None
        queryset = StateMachine.objects.select_related("default_version", "scope")
        machine = queryset.filter(key=key, scope=scope).first()
        if machine is None:
            where = f" in scope {scope_key!r}" if scope_key else " (global)"
            raise CommandError(f"No state machine with key {key!r}{where}.")
        return machine

    def _resolve(self, machine: StateMachine, label: str | None) -> StateMachineVersion:
        if label:
            try:
                return machine.versions.get(version=label)
            except StateMachineVersion.DoesNotExist:
                raise CommandError(f"{machine.key} has no version {label!r}.") from None
        default = machine.default_version
        if default is None:
            raise CommandError(f"{machine.key} has no default version; pass --label.")
        return default


def _serialize_hook(hook: StateMachineHook) -> dict[str, Any]:
    edge = hook.transition
    source = edge.from_state if edge is not None else None
    return {
        "handler": hook.handler_key,
        "timing": hook.timing,
        "event": hook.event,
        "transition": edge.name if edge is not None else None,
        "from": source.status.key if source is not None else None,
        "state": hook.state.status.key if hook.state is not None else None,
        "params": hook.params,
        "order": hook.order,
        "on_commit": hook.on_commit,
        "is_active": hook.is_active,
    }


def _serialize(machine: StateMachine, version: StateMachineVersion) -> dict[str, Any]:
    states = list(version.states.select_related("status").order_by("order", "pk"))
    transitions = list(
        version.transitions.select_related("action_type", "from_state__status", "to_state__status")
    )
    payload: dict[str, Any] = {"key": machine.key}
    scope = machine.scope
    if scope is not None:
        payload["scope"] = scope.scope_key
    return {
        **payload,
        "entity_type": machine.entity_type,
        "status_field": machine.status_field,
        "name": machine.name,
        "description": machine.description,
        "version": version.version,
        "notes": version.notes,
        "states": [
            {
                "key": state.status.key,
                "name": state.status.name,
                "description": state.status.description,
                "is_initial": state.is_initial,
                "is_terminal": state.is_terminal,
                "color": state.color,
                "order": state.order,
                "x": state.x,
                "y": state.y,
            }
            for state in states
        ],
        "transitions": [
            {
                "name": edge.name,
                "from": edge.from_state.status.key if edge.from_state is not None else None,
                "to": edge.to_state.status.key,
                "action": edge.action_type.key,
                "guard": edge.guard,
                "required_permission": edge.required_permission,
                "requires_approval": edge.requires_approval,
                "order": edge.order,
                "description": edge.description,
            }
            for edge in transitions
        ],
        "hooks": [
            _serialize_hook(hook)
            for hook in version.hooks.select_related(
                "state__status",
                "transition__action_type",
                "transition__from_state__status",
                "transition__to_state__status",
            ).order_by("order", "pk")
        ],
    }
