"""Authoring operations: validate a draft, publish it, clone it, retire it.

Publishing is the one moment where a graph is checked as a whole, because after it a
version is immutable and records will pin it for as long as they live.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from django.db import models, transaction
from django.utils import timezone

from vinta_state_machines.enums import HookEvent, Lifecycle
from vinta_state_machines.exceptions import InvalidVersionState
from vinta_state_machines.fields import get_status_field_config
from vinta_state_machines.graph import CREATION, VersionGraph, build_graph, invalidate_graph
from vinta_state_machines.guards import GuardSyntaxError, validate_guard
from vinta_state_machines.identities import resolve_identity
from vinta_state_machines.models import (
    ActionType,
    StateMachine,
    StateMachineHook,
    StateMachineState,
    StateMachineTransition,
    StateMachineVersion,
    StatusDefinition,
)
from vinta_state_machines.registry import NotRegistered
from vinta_state_machines.scopes import scope_from_key
from vinta_state_machines.side_effects import side_effect_registry

__all__ = [
    "ValidationReport",
    "archive_version",
    "clone_version",
    "next_version_label",
    "publish_version",
    "rebase_record",
    "set_default_version",
    "validate_version",
]

# The trailing number of a version label, which is the part that gets bumped.  Anchored
# at the end rather than parsing the label as a whole, so "2024.1" and "v3" both work.
_TRAILING_NUMBER = re.compile(r"^(?P<stem>.*?)(?P<number>\d+)$")


@dataclass
class ValidationReport:
    """The outcome of checking one version's graph."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def __bool__(self) -> bool:
        return self.ok

    def raise_if_invalid(self, version: StateMachineVersion) -> None:
        if self.errors:
            joined = "\n  - ".join(self.errors)
            raise InvalidVersionState(
                f"{version.state_machine.key}@{version.version} is not publishable:\n  - {joined}"
            )


def validate_version(
    version: StateMachineVersion, *, check_handlers: bool = True
) -> ValidationReport:
    """Check that a version's graph is internally consistent and runnable.

    Errors block publication.  Warnings — an unreachable state, a state with no way out
    that is not marked terminal — are surfaced but do not.
    """
    report = ValidationReport()
    graph = build_graph(version)

    if not graph.states:
        report.errors.append("The version declares no states.")
        return report
    if not graph.initial_states:
        report.errors.append("The version declares no initial state.")

    _check_cross_version_rows(version, report)
    _check_edges(graph, report)
    _check_guards(version, report)
    if check_handlers:
        _check_handlers(version, report)
    _check_reachability(graph, report)
    return report


def _check_cross_version_rows(version: StateMachineVersion, report: ValidationReport) -> None:
    """Every transition and hook must point at rows of its own version."""
    state_pks = set(version.states.values_list("pk", flat=True))
    for transition in version.transitions.all():
        if transition.to_state_id not in state_pks:
            report.errors.append(
                f"Transition {transition.pk} points to a state of another version."
            )
        if transition.from_state_id and transition.from_state_id not in state_pks:
            report.errors.append(
                f"Transition {transition.pk} starts from a state of another version."
            )
    transition_pks = set(version.transitions.values_list("pk", flat=True))
    for hook in version.hooks.all():
        if hook.event == HookEvent.TRANSITION and hook.transition_id not in transition_pks:
            report.errors.append(
                f"Hook {hook.handler_key!r} is bound to a transition of another version."
            )
        if hook.state_id and hook.state_id not in state_pks:
            report.errors.append(
                f"Hook {hook.handler_key!r} is bound to a state of another version."
            )


def _check_edges(graph: VersionGraph, report: ValidationReport) -> None:
    for spec in graph.transitions:
        if spec.from_key is CREATION:
            target = graph.state(spec.to_key)
            if target is not None and not target.is_initial:
                report.errors.append(
                    f"Creation edge --{spec.action}--> {spec.to_key} targets a state that "
                    "is not marked initial."
                )
            continue
        source = graph.state(spec.from_key)
        if source is not None and source.is_terminal:
            report.errors.append(
                f"{source.key} is terminal but has the outgoing transition {spec}."
            )


def _check_guards(version: StateMachineVersion, report: ValidationReport) -> None:
    for transition in version.transitions.select_related("action_type"):
        if not transition.guard:
            continue
        try:
            validate_guard(transition.guard)
        except (GuardSyntaxError, NotRegistered) as exc:
            report.errors.append(f"Guard of transition {transition.pk} is unusable: {exc}")


def _check_handlers(version: StateMachineVersion, report: ValidationReport) -> None:
    for hook in version.hooks.filter(is_active=True):
        if hook.handler_key not in side_effect_registry:
            report.errors.append(
                f"Hook {hook.pk} references the side effect {hook.handler_key!r}, "
                "which no installed app registers."
            )


def _check_reachability(graph: VersionGraph, report: ValidationReport) -> None:
    reachable = {state.key for state in graph.initial_states}
    reachable.update(spec.to_key for spec in graph.transitions_from(CREATION))
    queue = deque(reachable)
    while queue:
        for spec in graph.transitions_from(queue.popleft()):
            if spec.to_key not in reachable:
                reachable.add(spec.to_key)
                queue.append(spec.to_key)
    for state in graph.ordered_states:
        if state.key not in reachable:
            report.warnings.append(f"State {state.key} is unreachable.")
        if not state.is_terminal and not graph.transitions_from(state.key):
            report.warnings.append(
                f"State {state.key} has no outgoing transition but is not marked terminal."
            )


@transaction.atomic
def publish_version(
    version: StateMachineVersion,
    *,
    make_default: bool = True,
    author: Any = None,
    validate: bool = True,
    when: Any = None,
) -> ValidationReport:
    """Freeze a draft and, by default, make it what new records pin.

    Existing records are untouched: they keep validating against the version they
    already pinned.  Returns the validation report so callers can surface warnings.
    """
    if version.lifecycle != Lifecycle.DRAFT:
        raise InvalidVersionState(
            f"Only drafts can be published; {version.state_machine.key}@{version.version} "
            f"is {version.lifecycle}."
        )
    report = validate_version(version) if validate else ValidationReport()
    report.raise_if_invalid(version)

    version.mark_published(when=when or timezone.now())
    if author is not None and version.author_id is None:
        # Snapshotted here rather than linked, so "who published this, and what were
        # they allowed to do" stays answerable after their permissions change.
        version.author = resolve_identity(author)
    version.save(update_fields=["lifecycle", "published_at", "author", "modified_at"])
    invalidate_graph(version.pk)

    if make_default:
        set_default_version(version.state_machine, version)
    return report


@transaction.atomic
def set_default_version(machine: StateMachine, version: StateMachineVersion) -> None:
    """Point a machine's ``default_version`` at ``version``."""
    if version.state_machine_id != machine.pk:
        raise InvalidVersionState(
            f"{version.version} does not belong to the state machine {machine.key}."
        )
    if version.lifecycle != Lifecycle.PUBLISHED:
        raise InvalidVersionState(
            f"{machine.key}@{version.version} is {version.lifecycle}; only a published "
            "version can be the default."
        )
    machine.default_version = version
    machine.save(update_fields=["default_version", "modified_at"])


@transaction.atomic
def archive_version(version: StateMachineVersion, *, replacement: Any = None) -> None:
    """Retire a version.

    Records that pinned it keep working — the graph is still there — but it stops being
    what new records get.  If it is the machine's default, a ``replacement`` is required.
    """
    machine = version.state_machine
    if machine.default_version_id == version.pk:
        if replacement is None:
            raise InvalidVersionState(
                f"{machine.key}@{version.version} is the default version; pass "
                "replacement=... to archive it."
            )
        set_default_version(machine, replacement)
    version.lifecycle = Lifecycle.ARCHIVED
    version.save(update_fields=["lifecycle", "modified_at"])
    invalidate_graph(version.pk)


def next_version_label(machine: StateMachine, *, after: str | None = None) -> str:
    """The label the machine's next version should carry.

    Bumps the trailing number of the label it follows -- ``"1"`` to ``"2"``, ``"2024.1"``
    to ``"2024.2"``, ``"v3"`` to ``"v4"`` -- and falls back to appending one when there
    is no number to bump.  Labels already taken are skipped, so this stays usable on a
    machine whose versions were not numbered in order.

    Args:
        machine: The machine the version belongs to.
        after: The label to follow.  Defaults to the machine's latest version.

    Returns:
        A label no version of ``machine`` is using yet.
    """
    taken = set(machine.versions.values_list("version", flat=True))
    if after is None:
        latest = machine.latest_version()
        after = latest.version if latest is not None else "0"

    match = _TRAILING_NUMBER.match(after)
    if match is None:
        stem, number = f"{after}-", 1
    else:
        stem, number = match["stem"], int(match["number"])

    candidate = f"{stem}{number + 1}"
    while candidate in taken:
        number += 1
        candidate = f"{stem}{number + 1}"
    return candidate


@transaction.atomic
def clone_version(
    version: StateMachineVersion,
    new_label: str,
    *,
    author: Any = None,
    notes: str = "",
) -> StateMachineVersion:
    """Deep copy a version's states, transitions and hooks into a fresh draft.

    This is how you author version *n+1*: clone, edit the draft, publish.
    """
    clone = StateMachineVersion.objects.create(
        state_machine=version.state_machine,
        version=new_label,
        lifecycle=Lifecycle.DRAFT,
        author=None if author is None else resolve_identity(author),
        notes=notes,
    )
    state_map: dict[int, StateMachineState] = {}
    for state in version.states.all():
        state_map[state.pk] = StateMachineState.objects.create(
            state_machine_version=clone,
            status=state.status,
            is_initial=state.is_initial,
            is_terminal=state.is_terminal,
            color=state.color,
            order=state.order,
            x=state.x,
            y=state.y,
        )
    transition_map: dict[int, StateMachineTransition] = {}
    for edge in version.transitions.all():
        transition_map[edge.pk] = StateMachineTransition.objects.create(
            state_machine_version=clone,
            name=edge.name,
            from_state=state_map[edge.from_state_id] if edge.from_state_id else None,
            to_state=state_map[edge.to_state_id],
            action_type_id=edge.action_type_id,
            guard=edge.guard,
            required_permission=edge.required_permission,
            requires_approval=edge.requires_approval,
            order=edge.order,
            description=edge.description,
            label_offset_x=edge.label_offset_x,
            label_offset_y=edge.label_offset_y,
        )
    for hook in version.hooks.all():
        StateMachineHook.objects.create(
            state_machine_version=clone,
            handler_key=hook.handler_key,
            timing=hook.timing,
            event=hook.event,
            transition=transition_map[hook.transition_id] if hook.transition_id else None,
            state=state_map[hook.state_id] if hook.state_id else None,
            params=dict(hook.params or {}),
            order=hook.order,
            is_active=hook.is_active,
            on_commit=hook.on_commit,
            description=hook.description,
        )
    return clone


@transaction.atomic
def rebase_record(
    instance: models.Model,
    to_version: StateMachineVersion,
    field_name: str = "status_key",
    *,
    map_status: dict[str, str] | None = None,
    save: bool = True,
) -> models.Model:
    """Explicitly move one record onto a newer version.

    Nothing does this automatically — that is the whole point of pinning — so this is the
    deliberate, opt-in migration path.  The record's current status must exist as a state
    of the target version, or be renamed through ``map_status``.
    """
    config = get_status_field_config(type(instance), field_name)
    graph = to_version.graph()
    if graph.machine_key != config.machine_key:
        raise InvalidVersionState(
            f"{to_version} governs {graph.machine_key!r}, not {config.machine_key!r}."
        )
    current = getattr(instance, field_name, None) or None
    target = (map_status or {}).get(current, current) if current else None
    if target is not None and not graph.has_state(target):
        raise InvalidVersionState(
            f"{target!r} is not a state of {graph.machine_key}@{graph.version_label}; "
            "pass map_status to rename it."
        )
    setattr(instance, config.version_field, to_version)
    touched = [config.version_field]
    if target != current:
        setattr(instance, field_name, target or "")
        touched.append(field_name)
    if save and instance.pk is not None:
        instance.save(update_fields=touched)
    return instance


# ------------------------------------------------------------- declarative authoring


@transaction.atomic
def define_machine(definition: dict[str, Any], *, author: Any = None) -> StateMachineVersion:
    """Create a machine, its vocabulary and a draft version from a plain dict.

    This is the shape the ``import_state_machine`` command reads, and the most convenient
    way to seed machines from a data migration::

        define_machine(
            {
                "key": "risk.status",
                "scope": None,  # or a scope key, for a tenant specific machine
                "entity_type": "risk",
                "status_field": "status",
                "name": "Risk status",
                "version": "1",
                "states": [
                    {"key": "open", "name": "Open", "is_initial": True},
                    {"key": "closed", "name": "Closed", "is_terminal": True},
                ],
                "transitions": [
                    {"from": "open", "to": "closed", "action": "risk.close"},
                ],
            }
        )
    """
    machine, _created = StateMachine.objects.update_or_create(
        key=definition["key"],
        scope=scope_from_key(definition.get("scope")),
        defaults={
            "entity_type": definition["entity_type"],
            "status_field": definition.get("status_field", "status"),
            "name": definition.get("name", definition["key"]),
            "description": definition.get("description", ""),
        },
    )
    identity = None if author is None else resolve_identity(author)
    if identity is not None and machine.author_id is None:
        machine.author = identity
        machine.save(update_fields=["author", "modified_at"])
    version = StateMachineVersion.objects.create(
        state_machine=machine,
        version=str(definition.get("version", "1")),
        lifecycle=Lifecycle.DRAFT,
        author=identity,
        notes=definition.get("notes", ""),
    )

    states: dict[str, StateMachineState] = {}
    for order, spec in enumerate(definition.get("states", [])):
        status, _ = StatusDefinition.objects.update_or_create(
            entity_type=machine.entity_type,
            status_field=machine.status_field,
            key=spec["key"],
            defaults={
                "name": spec.get("name", spec["key"]),
                "description": spec.get("description", ""),
            },
        )
        states[spec["key"]] = StateMachineState.objects.create(
            state_machine_version=version,
            status=status,
            is_initial=spec.get("is_initial", False),
            is_terminal=spec.get("is_terminal", False),
            color=spec.get("color", "neutral"),
            order=spec.get("order", order),
            x=spec.get("x", 0),
            y=spec.get("y", 0),
        )

    transitions: list[StateMachineTransition] = []
    for order, spec in enumerate(definition.get("transitions", [])):
        action, _ = ActionType.objects.get_or_create(
            key=spec["action"],
            defaults={"name": spec.get("action_name", spec["action"])},
        )
        transitions.append(
            StateMachineTransition.objects.create(
                state_machine_version=version,
                # An unnamed edge falls back to its action, which is unambiguous as long
                # as one state does not carry two edges under the same action.
                name=spec.get("name") or spec["action"],
                from_state=states[spec["from"]] if spec.get("from") else None,
                to_state=states[spec["to"]],
                action_type=action,
                guard=spec.get("guard", ""),
                required_permission=spec.get("required_permission", ""),
                requires_approval=spec.get("requires_approval", False),
                order=spec.get("order", order),
                description=spec.get("description", ""),
            )
        )

    for order, spec in enumerate(definition.get("hooks", [])):
        event = spec.get("event", HookEvent.TRANSITION)
        edge = None
        if event == HookEvent.TRANSITION:
            edge = _match_transition(transitions, spec)
        StateMachineHook.objects.create(
            state_machine_version=version,
            handler_key=spec["handler"],
            timing=spec.get("timing", "after"),
            event=event,
            transition=edge,
            state=states[spec["state"]] if spec.get("state") else None,
            params=spec.get("params", {}),
            order=spec.get("order", order),
            is_active=spec.get("is_active", True),
            on_commit=spec.get("on_commit", False),
            description=spec.get("description", ""),
        )
    return version


def _match_transition(
    transitions: list[StateMachineTransition], spec: dict[str, Any]
) -> StateMachineTransition:
    """Find the edge a hook is bound to, by name — which is what makes it unambiguous.

    Matching on ``(from, to, action)`` would not do any more: one pair of states may be
    joined by several edges, and several may share an action.
    """
    wanted = spec.get("transition")
    if not wanted:
        raise InvalidVersionState(
            f"Hook {spec['handler']!r} is bound to a transition but does not name one; "
            "add a 'transition' key."
        )
    matches = [edge for edge in transitions if edge.name == wanted]
    if "from" in spec:
        source = spec["from"] or None
        matches = [edge for edge in matches if _source_key(edge) == source]
    if not matches:
        known = ", ".join(sorted(edge.name for edge in transitions)) or "<none>"
        raise InvalidVersionState(
            f"Hook {spec['handler']!r} names the transition {wanted!r}, which this version "
            f"does not declare. Available: {known}."
        )
    if len(matches) > 1:
        # Names are unique per source state, so two states may both declare "approve".
        sources = ", ".join(sorted(str(_source_key(edge)) for edge in matches))
        raise InvalidVersionState(
            f"Hook {spec['handler']!r} names the transition {wanted!r}, which leaves more "
            f"than one state ({sources}); add a 'from' key to say which."
        )
    return matches[0]


def _source_key(edge: StateMachineTransition) -> str | None:
    return edge.from_state.status.key if edge.from_state is not None else None
