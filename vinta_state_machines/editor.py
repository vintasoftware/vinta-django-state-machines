"""Translate a version's graph to and from the JSON the canvas editor speaks.

The editor (``vinta-state-machine-editor`` on npm) models a machine as states with a
position and four ordered side-effect lists, transitions with a trigger, a guard and
two more lists, and machine-level lists of initial and final state ids.  That lines up
with this app almost field for field; this module is where the remaining differences
are spelled out rather than left implicit:

* Elements are addressed by the editor's ``id`` throughout — a state's is its
  :class:`~vinta_state_machines.models.StatusDefinition` key, a transition's and a
  side effect's is the primary key of its row as a string.  Something just drawn on
  the canvas arrives with a generated ``state_``/``transition_``/``effect_`` id
  instead, which is how a new element is told from an edited one.  A new state's
  vocabulary key is slugified from its name, since a generated id makes a poor
  business identifier.
* Ordering is positional.  A state's ``order`` is its index, a transition's is its
  index among the edges leaving the same state, and a hook's is its index in the list
  it was dropped into — so dragging is all the UI has to offer for it.
* Whatever has no counterpart in the editor's model travels in the host-owned ``data``
  blob it hands back untouched: ``requires_approval`` on an edge, ``on_commit`` on a
  hook binding.

Hooks bound to :attr:`~vinta_state_machines.enums.HookEvent.ANY_TRANSITION` have no
place on a canvas that draws one card per edge.  They are invisible to the editor and
:func:`apply_editor_machine` leaves them alone, so they survive a round trip.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.models import ProtectedError
from django.utils.text import slugify

from vinta_state_machines.enums import HookEvent, HookTiming, StateColor
from vinta_state_machines.exceptions import StateMachineError
from vinta_state_machines.graph import invalidate_graph
from vinta_state_machines.guards import GuardSyntaxError, validate_guard
from vinta_state_machines.models import (
    KEY_REGEX,
    ActionType,
    StateMachine,
    StateMachineHook,
    StateMachineState,
    StateMachineTransition,
    StateMachineVersion,
    StatusDefinition,
)
from vinta_state_machines.registry import NotRegistered
from vinta_state_machines.side_effects import side_effect_catalog

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "EditorPayloadError",
    "action_catalog",
    "apply_editor_machine",
    "check_guard",
    "side_effect_definitions",
    "to_editor_machine",
]

STATE_ID_PREFIX = "state_"
TRANSITION_ID_PREFIX = "transition_"
EFFECT_ID_PREFIX = "effect_"

_TIMINGS = (HookTiming.BEFORE.value, HookTiming.AFTER.value)


class EditorPayloadError(StateMachineError):
    """The canvas sent something this version cannot be reconciled with.

    Carries every problem found rather than only the first, so the UI can show them
    all at once instead of one reload at a time.
    """

    default_code = "editor_payload_invalid"

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(" ".join(errors))


# ------------------------------------------------------------------ serialization


def _effects_payload(
    hooks: list[StateMachineHook], names: dict[str, str]
) -> dict[str, list[dict[str, Any]]]:
    """One ordered ``{"before": [...], "after": [...]}`` pair from a list of hook rows."""
    payload: dict[str, list[dict[str, Any]]] = {"before": [], "after": []}
    for hook in sorted(hooks, key=lambda row: (row.order, row.pk)):
        payload[hook.timing].append(
            {
                "id": str(hook.pk),
                "definitionId": hook.handler_key,
                "name": names.get(hook.handler_key, hook.handler_key),
                "params": hook.params,
                "enabled": hook.is_active,
                "description": hook.description,
                "data": {"on_commit": hook.on_commit},
            }
        )
    return payload


def to_editor_machine(version: StateMachineVersion) -> dict[str, Any]:
    """Serialize one version as the ``StateMachine`` document the editor consumes."""
    names = {info.key: info.name for info in side_effect_catalog()}

    states = list(version.states.select_related("status").order_by("order", "pk"))
    transitions = list(
        version.transitions.select_related(
            "action_type", "from_state__status", "to_state__status"
        ).order_by("order", "pk")
    )

    entered: dict[int, list[StateMachineHook]] = {}
    left: dict[int, list[StateMachineHook]] = {}
    on_edge: dict[int, list[StateMachineHook]] = {}
    for hook in version.hooks.all():
        if hook.event == HookEvent.TRANSITION and hook.transition_id is not None:
            on_edge.setdefault(hook.transition_id, []).append(hook)
        elif hook.state_id is None:
            continue
        elif hook.event == HookEvent.ENTER_STATE:
            entered.setdefault(hook.state_id, []).append(hook)
        elif hook.event == HookEvent.LEAVE_STATE:
            left.setdefault(hook.state_id, []).append(hook)

    return {
        "states": [
            {
                "id": state.status.key,
                "name": state.status.name,
                "position": {"x": state.x, "y": state.y},
                "color": state.color,
                "description": state.status.description,
                "onEnter": _effects_payload(entered.get(state.pk, []), names),
                "onLeave": _effects_payload(left.get(state.pk, []), names),
                "data": {},
            }
            for state in states
        ],
        "transitions": [
            {
                "id": str(edge.pk),
                "name": edge.name,
                "from": edge.from_state.status.key if edge.from_state is not None else None,
                "to": edge.to_state.status.key,
                "trigger": {"id": edge.action_type.key, "name": edge.action_type.name},
                "guard": edge.guard,
                "requiredPermission": edge.required_permission,
                "description": edge.description,
                "labelOffset": {"x": edge.label_offset_x, "y": edge.label_offset_y},
                "effects": _effects_payload(on_edge.get(edge.pk, []), names),
                "data": {"requires_approval": edge.requires_approval},
            }
            for edge in transitions
        ],
        "initialStateIds": [state.status.key for state in states if state.is_initial],
        "finalStateIds": [state.status.key for state in states if state.is_terminal],
        "data": {"machine": version.state_machine.key, "version": version.version},
    }


# ----------------------------------------------------------------------- catalogs


def side_effect_definitions() -> list[dict[str, Any]]:
    """The registered handlers, as the editor's ``SideEffectProvider`` payload."""
    return [
        {
            "id": info.key,
            "name": info.name,
            "description": info.description,
            "defaultParams": info.default_params,
        }
        for info in side_effect_catalog()
    ]


def action_catalog() -> list[dict[str, Any]]:
    """The action vocabulary, as the editor's ``ActionProvider`` payload."""
    return [
        {"id": action.key, "name": action.name, "description": action.description}
        for action in ActionType.objects.all()
    ]


def check_guard(expression: str) -> dict[str, Any]:
    """The editor's ``GuardValidator`` verdict for one expression."""
    if not expression.strip():
        return {"ok": True}
    try:
        validate_guard(expression)
    except (GuardSyntaxError, NotRegistered) as exc:
        return {"ok": False, "errors": [str(exc)]}
    return {"ok": True}


# ----------------------------------------------------------------------- applying


def _as_list(value: Any, label: str, errors: list[str]) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{label} must be an array.")
        return []
    return value


def _dicts(value: Any, label: str, errors: list[str]) -> list[dict[str, Any]]:
    """The object entries of a list, complaining once about anything else."""
    items = _as_list(value, label, errors)
    kept = [item for item in items if isinstance(item, dict)]
    if len(kept) != len(items):
        errors.append(f"Every entry of {label} must be an object.")
    return kept


def _existing_pk(raw_id: Any, prefix: str) -> int | None:
    """The row this element already has, or ``None`` when the canvas just drew it."""
    if not isinstance(raw_id, str) or raw_id.startswith(prefix):
        return None
    try:
        return int(raw_id)
    except ValueError:
        return None


def _status_key(spec: dict[str, Any], taken: set[str]) -> str:
    """The vocabulary key for a state: its id, or a slug of its name when newly drawn."""
    raw_id = spec.get("id")
    if isinstance(raw_id, str) and raw_id and not raw_id.startswith(STATE_ID_PREFIX):
        return raw_id
    base = slugify(str(spec.get("name") or "")) or "state"
    key = base
    suffix = 2
    while key in taken:
        key = f"{base}-{suffix}"
        suffix += 1
    return key


def _iter_effect_lists(
    states: list[dict[str, Any]], transitions: list[dict[str, Any]]
) -> Iterator[tuple[str, dict[str, Any], str, list[Any]]]:
    """Every ordered side-effect list in the document, with what it hangs off.

    Yields ``(kind, owner, timing, effects)``, where ``kind`` is ``onEnter``,
    ``onLeave`` or ``transition``.
    """
    for state in states:
        for trigger in ("onEnter", "onLeave"):
            hooks = state.get(trigger)
            if not isinstance(hooks, dict):
                continue
            for timing in _TIMINGS:
                effects = hooks.get(timing)
                if isinstance(effects, list):
                    yield trigger, state, timing, effects
    for edge in transitions:
        hooks = edge.get("effects")
        if not isinstance(hooks, dict):
            continue
        for timing in _TIMINGS:
            effects = hooks.get(timing)
            if isinstance(effects, list):
                yield "transition", edge, timing, effects


@transaction.atomic
def apply_editor_machine(version: StateMachineVersion, payload: Any) -> StateMachineVersion:
    """Reconcile a draft version with a machine document from the canvas.

    Rows are matched by id and updated in place, so primary keys — and the hooks and
    history pointing at them — survive an edit.  Anything the document no longer
    mentions is deleted.  Published and archived versions are immutable and are
    refused outright.
    """
    if not version.is_editable:
        raise EditorPayloadError(
            [f"{version} is {version.lifecycle} and can no longer be edited."]
        )
    if not isinstance(payload, dict):
        raise EditorPayloadError(["The machine must be an object."])

    errors: list[str] = []
    stamp = payload.get("data")
    if isinstance(stamp, dict):
        posted = stamp.get("machine")
        if posted is not None and posted != version.state_machine.key:
            errors.append(
                f"This document belongs to {posted!r}, not {version.state_machine.key!r}."
            )
    if errors:
        raise EditorPayloadError(errors)

    state_specs = _dicts(payload.get("states"), "machine.states", errors)
    edge_specs = _dicts(payload.get("transitions"), "machine.transitions", errors)
    initial = set(_as_list(payload.get("initialStateIds"), "initialStateIds", errors))
    final = set(_as_list(payload.get("finalStateIds"), "finalStateIds", errors))
    if errors:
        raise EditorPayloadError(errors)

    states = _apply_states(version, state_specs, initial, final, errors)
    if errors:
        raise EditorPayloadError(errors)

    transitions = _apply_transitions(version, edge_specs, states, errors)
    if errors:
        raise EditorPayloadError(errors)

    _apply_hooks(version, state_specs, edge_specs, states, transitions, errors)
    if errors:
        raise EditorPayloadError(errors)

    invalidate_graph(version.pk)
    return version


def _apply_states(
    version: StateMachineVersion,
    specs: list[dict[str, Any]],
    initial: set[Any],
    final: set[Any],
    errors: list[str],
) -> dict[str, StateMachineState]:
    """Upsert every state, keyed by the id the rest of the document refers to it by."""
    machine: StateMachine = version.state_machine
    existing = {state.status.key: state for state in version.states.select_related("status").all()}
    colors = set(StateColor.values)

    taken = set(existing)
    resolved: list[tuple[dict[str, Any], str]] = []
    for spec in specs:
        key = _status_key(spec, taken)
        taken.add(key)
        resolved.append((spec, key))

    result: dict[str, StateMachineState] = {}
    kept: set[int] = set()
    for order, (spec, key) in enumerate(resolved):
        editor_id = str(spec.get("id") or key)
        if not re.match(KEY_REGEX, key):
            errors.append(f"{key!r} cannot be used as a status key.")
            continue
        color = spec.get("color", StateColor.NEUTRAL)
        if color not in colors:
            errors.append(f"State {key!r} has the unknown colour {color!r}.")
            continue

        position = spec.get("position")
        position = position if isinstance(position, dict) else {}
        status, _ = StatusDefinition.objects.update_or_create(
            entity_type=machine.entity_type,
            status_field=machine.status_field,
            key=key,
            defaults={
                "name": str(spec.get("name") or key),
                "description": str(spec.get("description") or ""),
            },
        )
        state = existing.get(key) or StateMachineState(
            state_machine_version=version, status=status
        )
        state.status = status
        state.is_initial = editor_id in initial
        state.is_terminal = editor_id in final
        state.color = color
        state.order = order
        state.x = _as_int(position.get("x"))
        state.y = _as_int(position.get("y"))
        state.save()

        kept.add(state.pk)
        result[editor_id] = state

    stale = [state.pk for state in existing.values() if state.pk not in kept]
    if stale:
        StateMachineState.objects.filter(pk__in=stale).delete()
    return result


def _apply_transitions(
    version: StateMachineVersion,
    specs: list[dict[str, Any]],
    states: dict[str, StateMachineState],
    errors: list[str],
) -> dict[str, StateMachineTransition]:
    """Upsert every edge, ordering each among the edges that leave the same state."""
    existing = {edge.pk: edge for edge in version.transitions.all()}
    siblings: dict[str | None, int] = {}
    kept: set[int] = set()
    result: dict[str, StateMachineTransition] = {}

    for spec in specs:
        name = str(spec.get("name") or "").strip()
        if not name:
            errors.append("Every transition needs a name.")
            continue

        source_id = spec.get("from")
        if source_id is not None and source_id not in states:
            errors.append(f"Transition {name!r} leaves a state that is not in the document.")
            continue
        target_id = spec.get("to")
        if target_id not in states:
            errors.append(f"Transition {name!r} points at a state that is not in the document.")
            continue

        trigger = spec.get("trigger")
        if not isinstance(trigger, dict) or not trigger.get("id"):
            errors.append(f"Transition {name!r} has no trigger; pick an action for it.")
            continue
        action, _ = ActionType.objects.get_or_create(
            key=str(trigger["id"]),
            defaults={"name": str(trigger.get("name") or trigger["id"])},
        )

        guard = str(spec.get("guard") or "")
        verdict = check_guard(guard)
        if not verdict["ok"]:
            errors.append(f"Guard of transition {name!r} is unusable: {verdict['errors'][0]}")
            continue

        order = siblings.get(source_id, 0)
        siblings[source_id] = order + 1

        pk = _existing_pk(spec.get("id"), TRANSITION_ID_PREFIX)
        edge = existing.get(pk) if pk is not None else None
        if edge is None:
            edge = StateMachineTransition(state_machine_version=version)

        offset = spec.get("labelOffset")
        offset = offset if isinstance(offset, dict) else {}
        data = spec.get("data")
        data = data if isinstance(data, dict) else {}

        edge.name = name
        edge.from_state = states[source_id] if source_id is not None else None
        edge.to_state = states[target_id]
        edge.action_type = action
        edge.guard = guard
        edge.required_permission = str(spec.get("requiredPermission") or "")
        edge.requires_approval = bool(data.get("requires_approval", False))
        edge.description = str(spec.get("description") or "")
        edge.order = order
        edge.label_offset_x = _as_int(offset.get("x"))
        edge.label_offset_y = _as_int(offset.get("y"))
        edge.save()

        kept.add(edge.pk)
        result[str(spec.get("id"))] = edge

    stale = [pk for pk in existing if pk not in kept]
    if stale:
        try:
            StateMachineTransition.objects.filter(pk__in=stale).delete()
        except ProtectedError:
            errors.append("A transition cannot be removed because recorded history points at it.")
    return result


def _apply_hooks(
    version: StateMachineVersion,
    state_specs: list[dict[str, Any]],
    edge_specs: list[dict[str, Any]],
    states: dict[str, StateMachineState],
    transitions: dict[str, StateMachineTransition],
    errors: list[str],
) -> None:
    """Rebuild the six ordered side-effect lists per element into hook rows.

    ``any_transition`` hooks are not drawn on the canvas, so they are neither read
    from the document nor deleted for being absent from it.
    """
    existing = {
        hook.pk: hook for hook in version.hooks.all() if hook.event != HookEvent.ANY_TRANSITION
    }
    kept: set[int] = set()

    for kind, owner, timing, effects in _iter_effect_lists(state_specs, edge_specs):
        owner_id = str(owner.get("id"))
        state: StateMachineState | None = None
        edge: StateMachineTransition | None = None
        if kind == "transition":
            edge = transitions.get(owner_id)
            if edge is None:
                continue
            event = HookEvent.TRANSITION
        else:
            state = states.get(owner_id)
            if state is None:
                continue
            event = HookEvent.ENTER_STATE if kind == "onEnter" else HookEvent.LEAVE_STATE

        for order, spec in enumerate(effects):
            if not isinstance(spec, dict):
                errors.append("Every side effect must be an object.")
                continue
            handler = spec.get("definitionId")
            if not handler:
                errors.append("A side effect is missing the key of its handler.")
                continue

            pk = _existing_pk(spec.get("id"), EFFECT_ID_PREFIX)
            hook = existing.get(pk) if pk is not None else None
            if hook is None:
                hook = StateMachineHook(state_machine_version=version)

            params = spec.get("params")
            data = spec.get("data")
            data = data if isinstance(data, dict) else {}

            hook.handler_key = str(handler)
            hook.timing = timing
            hook.event = event
            hook.transition = edge
            hook.state = state
            hook.params = params if isinstance(params, dict) else {}
            hook.order = order
            hook.is_active = bool(spec.get("enabled", True))
            hook.on_commit = bool(data.get("on_commit", False))
            hook.description = str(spec.get("description") or "")
            hook.save()
            kept.add(hook.pk)

    stale = [pk for pk in existing if pk not in kept]
    if stale:
        StateMachineHook.objects.filter(pk__in=stale).delete()


def _as_int(value: Any) -> int:
    """Coordinates arrive as JSON numbers, which may be floats after a drag."""
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return 0
