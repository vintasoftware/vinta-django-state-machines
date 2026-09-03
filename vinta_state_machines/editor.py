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
from django.db.models import Count, ProtectedError
from django.utils.dateparse import parse_duration
from django.utils.duration import duration_iso_string
from django.utils.text import slugify

from vinta_state_machines.enums import HookEvent, HookTiming, Lifecycle, StateColor
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
from vinta_state_machines.services import (
    ValidationReport,
    next_version_label,
    publish_version,
    validate_version,
)
from vinta_state_machines.side_effects import side_effect_catalog

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "EditorPayloadError",
    "action_catalog",
    "apply_editor_machine",
    "check_editor_machine",
    "check_guard",
    "editor_machine_template",
    "empty_editor_machine",
    "publish_editor_machine",
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


def _visible(hooks: list[StateMachineHook]) -> list[StateMachineHook]:
    """Hooks the canvas draws as chips, which is every one it did not generate."""
    from vinta_state_machines.batch_effects import REPORT_HANDLER

    return [hook for hook in hooks if hook.handler_key != REPORT_HANDLER]


def _state_data(state: StateMachineState, entered: list[StateMachineHook]) -> dict[str, Any]:
    """The `data` bag: what a state carries beyond position, colour and name.

    ``counts_as`` is derived rather than stored, because the outcome lives on the hook
    row that declares it.  Handing the editor a value instead of a handler key is what
    keeps the canvas from having to match on ``"state_machines.report_to_batch"``.
    """
    from vinta_state_machines.batch_effects import REPORT_HANDLER

    data: dict[str, Any] = {}
    if state.is_waiting:
        data["is_waiting"] = True
        data["join_action"] = state.join_action.key if state.join_action is not None else ""
        data["child_machine"] = state.child_machine
        data["timeout"] = duration_iso_string(state.batch_timeout) if state.batch_timeout else ""
    for hook in entered:
        if hook.handler_key == REPORT_HANDLER:
            data["counts_as"] = hook.params.get("outcome", "success")
            break
    return data


def to_editor_machine(version: StateMachineVersion) -> dict[str, Any]:
    """Serialize one version as the ``StateMachine`` document the editor consumes."""
    names = {info.key: info.name for info in side_effect_catalog()}

    states = list(version.states.select_related("status", "join_action").order_by("order", "pk"))
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
                # The report pair is hidden from the lanes and surfaced as one
                # value: it is one concept stored as two rows, and an editor that
                # showed both chips would invite somebody to delete half of it.
                "onEnter": _effects_payload(_visible(entered.get(state.pk, [])), names),
                "onLeave": _effects_payload(_visible(left.get(state.pk, [])), names),
                "data": _state_data(state, entered.get(state.pk, [])),
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


def empty_editor_machine(machine: StateMachine | None = None) -> dict[str, Any]:
    """The document a canvas with nothing on it hands back.

    What the **add** form of a machine starts from: there is no version to serialize
    yet, and an editor assigned nothing at all would refuse the assignment.
    """
    return {
        "states": [],
        "transitions": [],
        "initialStateIds": [],
        "finalStateIds": [],
        "data": {} if machine is None else {"machine": machine.key},
    }


def editor_machine_template(machine: StateMachine) -> dict[str, Any]:
    """The document a *new* version of ``machine`` starts from.

    Its newest version's graph, so authoring version *n+1* starts from version *n*
    rather than from an empty canvas — the same move :func:`clone_version` makes for
    a version that already exists.

    "Newest" skips the versions with nothing on them.  A draft filed and never drawn
    is not what anybody means by *the previous setup*, and seeding from it would hand
    back the empty canvas this exists to avoid.

    Every row id is blanked first.  A state keeps its id, which is its vocabulary key
    and is shared across versions by design; a transition's and a side effect's are
    primary keys of rows belonging to the version it came from, and this document is
    going to be reconciled into a different one, where they mean nothing.
    """
    previous = (
        machine.versions.annotate(_states=Count("states"))
        .filter(_states__gt=0)
        .order_by("-created_at", "-pk")
        .first()
    )
    if previous is None:
        return empty_editor_machine(machine)
    document = to_editor_machine(previous)
    for index, edge in enumerate(document["transitions"]):
        edge["id"] = f"{TRANSITION_ID_PREFIX}{index}"
    for _kind, _owner, _timing, effects in _iter_effect_lists(
        document["states"], document["transitions"]
    ):
        for index, effect in enumerate(effects):
            effect["id"] = f"{EFFECT_ID_PREFIX}{index}"
    document["data"] = {"machine": machine.key}
    return document


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


def check_editor_machine(payload: Any) -> list[str]:
    """Every reason :func:`apply_editor_machine` would refuse ``payload``, read off the
    document alone.

    The rules that need the database are all about rows that are already there — an
    edge recorded history points at, a status key another version spelled differently
    — and a version that has just been created has none.  So for a **new** version
    this is the whole list, which is what lets the admin's add forms reject a graph
    while the person who drew it can still fix it, rather than after the version it
    was drawn for has been saved.

    Kept deliberately separate from :func:`apply_editor_machine` rather than wired
    into it: reconciling an existing draft is a settled path, and a pre-flight that
    turned out stricter than the reconciliation would start refusing documents that
    used to apply cleanly.  ``test_editor`` holds the two to the same answers.
    """
    if not isinstance(payload, dict):
        return ["The machine must be an object."]

    errors: list[str] = []
    state_specs = _dicts(payload.get("states"), "machine.states", errors)
    edge_specs = _dicts(payload.get("transitions"), "machine.transitions", errors)
    _as_list(payload.get("initialStateIds"), "initialStateIds", errors)
    _as_list(payload.get("finalStateIds"), "finalStateIds", errors)
    if errors:
        return errors

    colors = set(StateColor.values)
    taken: set[str] = set()
    state_ids: set[str] = set()
    for spec in state_specs:
        key = _status_key(spec, taken)
        taken.add(key)
        state_ids.add(str(spec.get("id") or key))
        if not re.match(KEY_REGEX, key):
            errors.append(f"{key!r} cannot be used as a status key.")
        color = spec.get("color", StateColor.NEUTRAL)
        if color not in colors:
            errors.append(f"State {key!r} has the unknown colour {color!r}.")

    for spec in edge_specs:
        name = str(spec.get("name") or "").strip()
        if not name:
            errors.append("Every transition needs a name.")
            continue
        source_id = spec.get("from")
        if source_id is not None and source_id not in state_ids:
            errors.append(f"Transition {name!r} leaves a state that is not in the document.")
            continue
        if spec.get("to") not in state_ids:
            errors.append(f"Transition {name!r} points at a state that is not in the document.")
            continue
        trigger = spec.get("trigger")
        if not isinstance(trigger, dict) or not trigger.get("id"):
            errors.append(f"Transition {name!r} has no trigger; pick an action for it.")
            continue
        verdict = check_guard(str(spec.get("guard") or ""))
        if not verdict["ok"]:
            errors.append(f"Guard of transition {name!r} is unusable: {verdict['errors'][0]}")

    for _kind, _owner, _timing, effects in _iter_effect_lists(state_specs, edge_specs):
        for spec in effects:
            if not isinstance(spec, dict):
                errors.append("Every side effect must be an object.")
            elif not spec.get("definitionId"):
                errors.append("A side effect is missing the key of its handler.")
    return errors


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
    _apply_counts_as(state_specs, states)
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
        _apply_state_data(state, spec.get("data"), errors)
        state.save()

        kept.add(state.pk)
        result[editor_id] = state

    stale = [state.pk for state in existing.values() if state.pk not in kept]
    if stale:
        StateMachineState.objects.filter(pk__in=stale).delete()
    return result


def _apply_state_data(state: StateMachineState, data: Any, errors: list[str]) -> None:
    """Read the fan-out declaration off the document's `data` bag.

    A document that says nothing leaves the state as it was, so a client built before
    any of this existed round trips unchanged.
    """
    if not isinstance(data, dict):
        return
    if "is_waiting" not in data:
        return

    state.is_waiting = bool(data.get("is_waiting"))
    state.child_machine = str(data.get("child_machine") or "")

    raw_timeout = data.get("timeout") or ""
    state.batch_timeout = parse_duration(raw_timeout) if raw_timeout else None
    if raw_timeout and state.batch_timeout is None:
        errors.append(f"State {state.status.key!r} has an unreadable timeout {raw_timeout!r}.")

    action_key = str(data.get("join_action") or "")
    if not action_key:
        state.join_action = None
        if state.is_waiting:
            errors.append(
                f"State {state.status.key!r} fans work out but names no join action, "
                "so nothing would ever move the record on."
            )
        return
    action = ActionType.objects.filter(key=action_key).first()
    if action is None:
        errors.append(f"State {state.status.key!r} names the unknown action {action_key!r}.")
        return
    state.join_action = action


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
    from the document nor deleted for being absent from it.  Report bindings are left
    out for the same reason: they are not chips, they are the state's ``counts_as``, and
    :func:`_apply_counts_as` owns them.
    """
    from vinta_state_machines.batch_effects import REPORT_HANDLER

    existing = {
        hook.pk: hook
        for hook in version.hooks.all()
        if hook.event != HookEvent.ANY_TRANSITION and hook.handler_key != REPORT_HANDLER
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


def _apply_counts_as(
    state_specs: list[dict[str, Any]], states: dict[str, StateMachineState]
) -> None:
    """Turn each state's ``counts_as`` back into the pair of report bindings.

    The editor never sees a handler key.  It sends a value -- ``"success"``,
    ``"failure"``, or nothing -- and this is where that becomes rows, which is what
    keeps the canvas from having to know the name of a function in this package.

    The ``leave_state`` half is written only where the state can actually be left. On a
    terminal state the engine refuses the move, so that binding could never fire.
    """
    from vinta_state_machines.batch_effects import REPORT_HANDLER

    for spec in state_specs:
        state = states.get(str(spec.get("id")))
        if state is None:
            continue
        data = spec.get("data")
        outcome = data.get("counts_as") if isinstance(data, dict) else None

        bindings = StateMachineHook.objects.filter(
            state_machine_version=state.state_machine_version,
            handler_key=REPORT_HANDLER,
            state=state,
        )
        if not outcome:
            bindings.delete()
            continue

        wanted = [HookEvent.ENTER_STATE]
        if not state.is_terminal:
            wanted.append(HookEvent.LEAVE_STATE)
        bindings.exclude(event__in=wanted).delete()
        for event in wanted:
            StateMachineHook.objects.update_or_create(
                state_machine_version=state.state_machine_version,
                handler_key=REPORT_HANDLER,
                event=event,
                state=state,
                defaults={
                    "timing": HookTiming.AFTER,
                    "on_commit": True,
                    "params": {"outcome": outcome},
                },
            )


def _as_int(value: Any) -> int:
    """Coordinates arrive as JSON numbers, which may be floats after a drag."""
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- publishing a revision


def _check_document_is_current(payload: Any, source: StateMachineVersion | None) -> None:
    """Refuse a canvas drawn on a version that is no longer the machine's latest.

    The document carries the label it was serialized from, so a graph edited in one tab
    while another published a version is caught here instead of silently landing on top
    of work it never saw.  A document with no stamp at all -- an empty canvas, which is
    what a machine with no versions starts from -- has nothing to be stale about.
    """
    stamp = payload.get("data") if isinstance(payload, dict) else None
    posted = stamp.get("version") if isinstance(stamp, dict) else None
    current = source.version if source is not None else None
    if posted is not None and posted != current:
        machine_key = source.state_machine.key if source is not None else "?"
        raise EditorPayloadError(
            [
                f"This canvas was loaded from version {posted!r}, but the latest version "
                f"of {machine_key} is now {current!r}. Reload before saving: publishing "
                "this would drop whatever was published in between."
            ]
        )


def _carry_over_offscreen_hooks(source: StateMachineVersion, draft: StateMachineVersion) -> None:
    """Copy the hooks the canvas cannot draw from one version to the next.

    The document is the whole truth about everything the editor puts on screen, so the
    draft is built from it rather than copied -- which is what keeps a transition from
    colliding with the copy of itself on the way in.  ``any_transition`` hooks belong to
    the version rather than to any one card, so they are never in the document and would
    otherwise be lost at each save.
    """
    for hook in source.hooks.filter(event=HookEvent.ANY_TRANSITION):
        StateMachineHook.objects.create(
            state_machine_version=draft,
            handler_key=hook.handler_key,
            timing=hook.timing,
            event=hook.event,
            params=dict(hook.params or {}),
            order=hook.order,
            is_active=hook.is_active,
            on_commit=hook.on_commit,
            description=hook.description,
        )


@transaction.atomic
def publish_editor_machine(
    machine: StateMachine,
    payload: Any,
    *,
    author: Any = None,
    label: str | None = None,
    notes: str = "",
) -> tuple[StateMachineVersion, ValidationReport]:
    """Publish a canvas document as the machine's next version.

    This is the save path behind the canvas on a *machine's* change form, where a graph
    is edited as one living thing rather than one version at a time.  Nothing existing
    is mutated: the document lands on a brand new draft, which is then published and
    made the default -- so records that pinned the old version go on validating against
    exactly the graph they pinned.

    Where the version add form asks for a label and files a draft, this asks for
    neither: it is the same move made in one gesture, for people who think in flows
    rather than in version rows.

    The draft is built from the document rather than cloned from the version it was
    drawn on, because the two describe the same graph and the rows would collide.  What
    the canvas cannot draw is carried across separately; see
    :func:`_carry_over_offscreen_hooks`.

    Args:
        machine: The machine to publish a new version of.
        payload: The document the canvas posted back.
        author: The principal to record as the publisher, snapshotted at this moment.
        label: The new version's label.  Defaults to bumping the latest one.
        notes: Notes for the new version.

    Returns:
        The published version and its validation report, whose warnings did not block.

    Raises:
        EditorPayloadError: The document is stale, cannot be reconciled, or describes a
            graph that is not publishable.  Nothing is written in any of those cases.
    """
    source = machine.latest_version()
    _check_document_is_current(payload, source)

    draft = StateMachineVersion.objects.create(
        state_machine=machine,
        version=label or next_version_label(machine),
        lifecycle=Lifecycle.DRAFT,
        notes=notes,
    )
    if source is not None:
        _carry_over_offscreen_hooks(source, draft)

    apply_editor_machine(draft, payload)

    # Validated here rather than left to ``publish_version`` so a graph the canvas drew
    # badly comes back in the same shape as every other editor complaint.
    report = validate_version(draft)
    if report.errors:
        raise EditorPayloadError(report.errors)
    publish_version(draft, author=author, validate=False)
    return draft, report
