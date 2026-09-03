"""Fan-out batches: a record waits on many children, then moves itself forward.

A state that starts a lot of work opens a batch.  Every child that finishes is counted
against it, and when the last one lands exactly one caller claims the batch and fires
the join -- an ordinary :func:`~vinta_state_machines.engine.transition` on the parent,
guarded and recorded like any other.

Children are ordinary status-bearing records, so a child that opens a batch of its own
is a parent one level down.  That is the whole of nesting.

One rule explains every write in this module:

    **The stamps are the truth. The counter is a fast approximation the sweeper
    repairs.**

A child is counted because it carries a stamp, not because somebody called something.
So the counting path stamps first and increments second, and the un-counting path
clears the stamp first and decrements second.  A crash in either gap leaves the counter
wrong and the stamps right, which is the half we know how to rebuild.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, models, transaction
from django.db.models import F
from django.utils import timezone

from vinta_state_machines.conf import batch_model_path, get_setting
from vinta_state_machines.enums import BatchFailureReason, BatchLifecycle, HookEvent
from vinta_state_machines.exceptions import BatchDepthExceeded, StateMachineError
from vinta_state_machines.fields import (
    batch_member_relations,
    get_batch_field_config,
    get_status_field_config,
    status_fields_of,
)
from vinta_state_machines.identities import resolve_identity

if TYPE_CHECKING:
    from datetime import timedelta

    from vinta_state_machines.models import StatusBatch

logger = logging.getLogger("vinta_state_machines.batches")

SUCCESS = "success"
FAILURE = "failure"

JOIN = "join"
CANCEL = "cancel"

CANCEL_ACTION_KEY = "child_cancel_action"
"""Where abandon() leaves the action for the cascade, which only gets an id."""

CANCEL_CHUNK = 500
"""How many children to pull at a time. A cancel can span a very large tree."""

__all__ = [
    "CANCEL",
    "FAILURE",
    "JOIN",
    "SUCCESS",
    "abandon",
    "batch_model",
    "count_child",
    "dispatch",
    "join_metadata",
    "live_batch_for",
    "open_batch",
    "outcomes_of",
    "recount",
    "report",
    "report_failure",
    "report_success",
    "run_batch_operation",
    "seal",
    "try_claim",
    "uncount_child",
]


def batch_model() -> Any:
    """The configured batch model, resolved late so a swap is honoured."""
    return apps.get_model(batch_model_path())


def _report_model() -> Any:
    from vinta_state_machines.models import StatusBatchReport

    return StatusBatchReport


# ------------------------------------------------------------------- opening


def live_batch_for(instance: models.Model, field_name: str = "status_key") -> StatusBatch | None:
    """The batch currently waiting on this record's status field, if any."""
    model = batch_model()
    found: StatusBatch | None = (
        model.objects.for_object(instance, status_field=field_name).live().first()
    )
    return found


def open_batch(
    instance: models.Model,
    *,
    join_action: str,
    field_name: str = "status_key",
    total: int | None = None,
    timeout: timedelta | None = None,
    parent_batch: StatusBatch | None = None,
    actor: Any = None,
    join_retry_after: timedelta | None = None,
    metadata: dict[str, Any] | None = None,
) -> StatusBatch:
    """Open the batch this record will wait on, or return the one already open.

    Idempotent by construction: at most one live batch per record per status field is a
    database constraint, so a hook that fires again after a retry finds the batch it
    already made rather than adding a second.

    ``total`` seals the batch immediately, which is what you want whenever the count is
    known up front.  Leaving it out means children are still being created, and nothing
    can complete until :func:`seal` says the total is final.

    ``actor`` is snapshotted **once**, here, and reused for every child move and for the
    join.  That is one identity row per fan-out rather than one per child, and it makes
    the whole run one queryable unit in the audit log.
    """
    from vinta_state_machines.engine import resolve_version
    from vinta_state_machines.models import ActionType, StatusDefinition

    existing = live_batch_for(instance, field_name)
    if existing is not None:
        return existing

    depth = 0 if parent_batch is None else parent_batch.depth + 1
    limit = get_setting("MAX_BATCH_DEPTH")
    if depth > limit:
        raise BatchDepthExceeded(
            f"Opening a batch on {instance._meta.label} would nest {depth} deep, past "
            f"MAX_BATCH_DEPTH ({limit}). A machine whose child machine is itself will "
            "do this; check the fan-out wiring before raising the cap."
        )

    version = resolve_version(instance, field_name)
    graph = version.graph()
    status_key = getattr(instance, field_name, None) or None
    if status_key is None:
        raise StateMachineError(
            f"{instance._meta.label} has no status on {field_name!r}, so there is no "
            "state for a batch to be opened in."
        )

    opened_in = StatusDefinition.objects.get(
        entity_type=graph.entity_type, status_field=graph.status_field, key=status_key
    )
    action = ActionType.objects.get(key=join_action)
    now = timezone.now()

    values = {
        "target_type": ContentType.objects.get_for_model(instance, for_concrete_model=False),
        "target_id": str(instance.pk),
        "status_field": field_name,
        "opened_in_status": opened_in,
        "state_machine_version": version,
        "join_action": action,
        "actor": resolve_identity(actor),
        "scope_key": graph.scope_key,
        "depth": depth,
        "parent_batch": parent_batch,
        "timeout_at": None if timeout is None else now + timeout,
        "join_retry_after": join_retry_after,
        "metadata": dict(metadata or {}),
        "total": total or 0,
        "sealed": total is not None,
    }

    model = batch_model()
    try:
        with transaction.atomic():
            created: StatusBatch = model.objects.create(**values)
            return created
    except IntegrityError:
        # Lost the race to another caller. The constraint did its job; use theirs.
        raced = live_batch_for(instance, field_name)
        if raced is None:  # pragma: no cover - the constraint says this cannot happen
            raise
        return raced


def seal(batch: StatusBatch, total: int | None = None) -> StatusBatch:
    """Fix the total and stop accepting new children.

    Counts the children itself when ``total`` is left out, using the foreign key they
    carry, so nobody maintains a total by hand and it cannot drift.

    Claims the batch as its last act.  Without that, a fan-out whose children all
    finished *before* the seal hangs forever: every one of them reported into a batch
    that was not yet allowed to be complete, so none of them was the last.
    """
    resolved = batch.count_members() if total is None else total
    batch_model().objects.filter(pk=batch.pk, lifecycle=BatchLifecycle.OPEN).update(
        sealed=True, total=resolved
    )
    batch.refresh_from_db()
    try_claim(batch.pk)
    return batch


# ------------------------------------------------------------------ counting


def count_child(child: models.Model, *, outcome: str, batch_field: str | None = None) -> bool:
    """Count ``child`` against its batch, once and only once.

    The stamp is what makes this safe to repeat.  A redelivered task, or a record
    pushed into a finished state a second time, finds the stamp already set and does
    nothing -- because a batch counts *children*, not moves.

    Returns:
        Whether this call was the one that counted it.
    """
    config = get_batch_field_config(type(child), batch_field)
    batch_id = getattr(child, f"{config.field_name}_id", None)
    if batch_id is None:
        return False

    model = type(child)
    stamped = model._default_manager.filter(
        pk=child.pk, **{f"{config.reported_at_field}__isnull": True}
    ).update(**{config.reported_at_field: timezone.now()})
    if not stamped:
        return False
    setattr(child, config.reported_at_field, timezone.now())

    updates: dict[str, Any] = {"finished": F("finished") + 1}
    if outcome == SUCCESS:
        updates["succeeded"] = F("succeeded") + 1
    # Only a live batch has a counter worth moving. Reporting into one that has been
    # abandoned is a no-op rather than an error: a worker already running when the
    # cancel landed will finish and report, every time.
    batch_model().objects.filter(pk=batch_id, lifecycle=BatchLifecycle.OPEN).update(**updates)

    try_claim(batch_id)
    return True


def uncount_child(child: models.Model, *, outcome: str, batch_field: str | None = None) -> bool:
    """Walk back the count for a child that has left its finished state.

    Only possible while the batch is still open, and only for a finished state that is
    not flagged terminal -- the engine refuses to leave one that is.  Once the batch is
    joining the parent has already been decided, and reopening a child after that is a
    new story rather than a correction to this one.

    Returns:
        Whether this call was the one that un-counted it.
    """
    config = get_batch_field_config(type(child), batch_field)
    batch_id = getattr(child, f"{config.field_name}_id", None)
    if batch_id is None:
        return False

    model = type(child)
    # The stamp goes first, mirroring the counting path. A crash in the gap leaves the
    # counter too high, which the sweeper's recount pulls back down; the other order
    # would leave a stamped-but-uncounted child and push the counter the wrong way.
    cleared = model._default_manager.filter(
        pk=child.pk, **{f"{config.reported_at_field}__isnull": False}
    ).update(**{config.reported_at_field: None})
    if not cleared:
        return False
    setattr(child, config.reported_at_field, None)

    filters: dict[str, Any] = {
        "pk": batch_id,
        "lifecycle": BatchLifecycle.OPEN,
        "finished__gt": 0,
    }
    updates: dict[str, Any] = {"finished": F("finished") - 1}
    if outcome == SUCCESS:
        # These are PositiveIntegerFields and the database refuses a negative, so the
        # filter has to say what the update assumes.
        filters["succeeded__gt"] = 0
        updates["succeeded"] = F("succeeded") - 1
    batch_model().objects.filter(**filters).update(**updates)
    return True


def report(batch: StatusBatch | int, key: str, outcome: str) -> bool:
    """Count one *opaque* unit of work -- a queue task with no record of its own.

    The key is required, so there is no unprotected path through here.  It has to name
    the work rather than the attempt: a task id dedupes a retry but not a re-enqueue,
    while a natural key from the source data dedupes both.

    Returns:
        Whether this call was the one that counted it.
    """
    batch_id = batch if isinstance(batch, int) else batch.pk
    if not key:
        raise ValueError(
            "report() needs a key naming this unit of work. Without one a retried "
            "task counts twice, and there is no record to carry a stamp instead."
        )

    _, created = _report_model().objects.get_or_create(
        batch_id=batch_id, key=key, defaults={"outcome": outcome}
    )
    if not created:
        return False

    updates: dict[str, Any] = {"finished": F("finished") + 1}
    if outcome == SUCCESS:
        updates["succeeded"] = F("succeeded") + 1
    batch_model().objects.filter(pk=batch_id, lifecycle=BatchLifecycle.OPEN).update(**updates)

    try_claim(batch_id)
    return True


def report_success(batch: StatusBatch | int, key: str) -> bool:
    """See :func:`report`."""
    return report(batch, key, SUCCESS)


def report_failure(batch: StatusBatch | int, key: str) -> bool:
    """See :func:`report`."""
    return report(batch, key, FAILURE)


# -------------------------------------------------------------------- claiming


def try_claim(batch_id: int, *, failure_reason: str = "", failure_detail: str = "") -> bool:
    """Claim the batch for exactly one caller, and dispatch the join if we won.

    A conditional ``UPDATE`` re-checks its condition after taking the row lock, so it
    matches only when this really is the last child *and* only for the first caller to
    ask.  Everybody else gets zero rows back and quietly does nothing.

    Passing a ``failure_reason`` claims a batch that is *not* complete, which is how a
    timeout ends one: the work never finished, but the parent still has to be moved out
    of the state it is waiting in.

    Returns:
        Whether this call claimed it.
    """
    filters: dict[str, Any] = {"pk": batch_id, "lifecycle": BatchLifecycle.OPEN}
    if not failure_reason:
        filters.update(sealed=True, finished=F("total"))

    claimed = (
        batch_model()
        .objects.filter(**filters)
        .update(
            lifecycle=BatchLifecycle.JOINING,
            joining_at=timezone.now(),
            failure_reason=failure_reason,
            failure_detail=failure_detail,
        )
    )
    if not claimed:
        return False
    dispatch(JOIN, batch_id)
    return True


def dispatch(operation: str, batch_id: int) -> None:
    """Hand one batch operation to wherever this project runs background work.

    Always deferred to commit, whichever dispatcher is configured.  The alternative is
    a worker reading a batch row that its own transaction has not written yet, which is
    a race no project should have to remember to avoid.
    """
    dispatcher = get_setting("BATCH_DISPATCHER")
    if dispatcher is None:
        transaction.on_commit(lambda: run_batch_operation(operation, batch_id))
        return
    transaction.on_commit(lambda: dispatcher(operation, batch_id))


def run_batch_operation(operation: str, batch_id: int) -> None:
    """Run one dispatched operation. Safe to call twice; the sweeper will.

    This is the entry point a queue task calls, which is why it takes an id and a
    string rather than anything that has to be pickled or can go stale in transit.
    """
    if operation == JOIN:
        _run_join(batch_id)
        return
    if operation == CANCEL:
        _run_cancel(batch_id)
        return
    raise ValueError(f"Unknown batch operation {operation!r}.")


# ----------------------------------------------------------------- the join


def _run_join(batch_id: int) -> None:
    """Move the parent, then close the batch. Idempotent, because retries happen."""
    from vinta_state_machines.engine import transition

    model = batch_model()
    batch = (
        model.objects.select_related("join_action", "actor", "state_machine_version")
        .filter(pk=batch_id)
        .first()
    )
    if batch is None or batch.lifecycle != BatchLifecycle.JOINING:
        # Already closed by an earlier run, or abandoned under us. Nothing to do.
        return

    target = batch.target
    if target is None:
        abandon(
            batch,
            reason=BatchFailureReason.JOIN_FAILED,
            detail="The record this batch waited on is gone.",
        )
        return

    try:
        transition(
            target,
            batch.join_action.key,
            batch.status_field,
            actor=batch.actor,
            metadata={"batch": join_metadata(batch)},
        )
    except StateMachineError:
        # Left in `joining` on purpose: the sweeper re-dispatches, and a graph fixed in
        # the meantime lets the retry succeed. A batch that can never join is given up
        # on by the sweeper rather than here, where we cannot know it is hopeless.
        logger.exception("Batch %s could not join %s", batch_id, batch.join_action.key)
        raise

    _close(batch_id)


def _close(batch_id: int) -> None:
    claimed = (
        batch_model()
        .objects.filter(pk=batch_id, lifecycle=BatchLifecycle.JOINING)
        .update(lifecycle=BatchLifecycle.CLOSED)
    )
    if claimed:
        # Reports do not outlive their batch, so the table only ever holds work that is
        # still in flight.
        _report_model().objects.filter(batch_id=batch_id).delete()


def abandon(
    batch: StatusBatch,
    *,
    reason: str,
    detail: str = "",
    child_cancel_action: str | None = None,
) -> bool:
    """Give up on a batch, so nothing it counts can ever move its parent.

    Always the first half of a cancellation, and not optional.  A batch left open after
    its parent has moved keeps taking reports, the last one claims the join, and the
    join fires at a record that no longer has that edge -- a ``TransitionNotAllowed``
    raised inside a background worker, long after anybody was watching.

    ``child_cancel_action`` additionally tells the children to stop.  That is dispatched
    rather than run here: cancelling a child is a real transition, which fires its own
    side effects and abandons its own batch one level further down, and there may be a
    million of them.

    Returns:
        Whether this call was the one that abandoned it.
    """
    updates: dict[str, Any] = {
        "lifecycle": BatchLifecycle.ABANDONED,
        "failure_reason": reason,
        "failure_detail": detail,
    }
    if child_cancel_action:
        updates["metadata"] = {**(batch.metadata or {}), CANCEL_ACTION_KEY: child_cancel_action}

    abandoned = (
        batch_model()
        .objects.filter(pk=batch.pk)
        .exclude(lifecycle__in=(BatchLifecycle.CLOSED, BatchLifecycle.ABANDONED))
        .update(**updates)
    )
    if not abandoned:
        return False

    _report_model().objects.filter(batch_id=batch.pk).delete()
    if child_cancel_action:
        dispatch(CANCEL, batch.pk)
    return True


def _run_cancel(batch_id: int) -> None:
    """Tell every unfinished child of an abandoned batch to stop.

    A child whose graph has no edge for the action is **left to run**.  Nothing raises,
    and no child machine is forced to grow a cancel edge it did not want; its eventual
    report lands in an abandoned batch and is ignored like any other.

    There is no publish-time check for a missing edge, and there cannot be: the parent's
    hook names an action key, not a child machine, so nothing in the catalog links the
    two.  One log line per machine and action is the most that can be said.
    """
    from vinta_state_machines.engine import transition

    batch = batch_model().objects.filter(pk=batch_id).first()
    if batch is None:
        return
    action = (batch.metadata or {}).get(CANCEL_ACTION_KEY)
    if not action:
        return

    skipped: set[str] = set()
    for relation in batch_member_relations(type(batch)):
        model = relation.related_model
        declared = status_fields_of(model)
        if not declared:
            continue  # nothing to move: no governed status to move it on
        field_name = declared[0].name
        stamp = get_batch_field_config(model, relation.field.name).reported_at_field
        unfinished = model._default_manager.filter(
            **{relation.field.name: batch_id, f"{stamp}__isnull": True}
        )
        for child in unfinished.iterator(chunk_size=CANCEL_CHUNK):
            try:
                transition(child, action, field_name, actor=batch.actor)
            except StateMachineError:
                label = f"{model._meta.label}:{action}"
                if label not in skipped:
                    skipped.add(label)
                    logger.info(
                        "Batch %s: %s declares no usable %r edge, so its children are "
                        "left running and their reports will be ignored.",
                        batch_id,
                        model._meta.label,
                        action,
                    )


def join_metadata(batch: StatusBatch) -> dict[str, Any]:
    """What the join hands to the guards on the parent's edges.

    Every key a guard could reasonably read is always present, including
    ``failure_reason`` as ``""`` when nothing went wrong.  A guard is a plain
    subscript with no ``.get``, so a missing key is a ``KeyError`` escaping through
    ``_assert_viable``, which only catches syntax errors -- an absent key would break
    the transition rather than fail the guard.
    """
    return {
        "id": batch.pk,
        "total": batch.total,
        "finished": batch.finished,
        "succeeded": batch.succeeded,
        "failed": batch.failed,
        "failure_reason": batch.failure_reason,
        "failure_detail": batch.failure_detail,
        "depth": batch.depth,
        "by_status": _by_status(batch),
    }


def _by_status(batch: StatusBatch) -> dict[str, int]:
    """Stamped children grouped by the status they finished on, zero-filled.

    Built once, by the single caller that won the claim, so the numbers are a real
    query rather than a counter that could have drifted.  Zero-filled from the graphs
    the children are actually pinned to, which is what keeps a guard naming a status
    that never occurred from raising.
    """
    from vinta_state_machines.graph import get_graph

    counts: dict[str, int] = {}
    keys: set[str] = set()

    for relation in batch_member_relations(type(batch)):
        child_model = relation.related_model
        declared = status_fields_of(child_model)
        if not declared:
            # A child with no governed status still counts toward the totals; it just
            # has no status to be grouped by.
            continue
        status_field = declared[0]
        config = get_status_field_config(child_model, status_field.name)
        stamp = get_batch_field_config(child_model, relation.field.name).reported_at_field
        rows = child_model._default_manager.filter(
            **{relation.field.name: batch.pk, f"{stamp}__isnull": False}
        )
        for row in rows.values(status_field.name).annotate(n=models.Count("pk")):
            key = row[status_field.name]
            counts[key] = counts.get(key, 0) + row["n"]
        for version_pk in (
            rows.exclude(**{f"{config.version_field}__isnull": True})
            .values_list(f"{config.version_field}_id", flat=True)
            .distinct()
        ):
            keys.update(get_graph(version_pk).states)

    return {key: counts.get(key, 0) for key in keys | set(counts)}


def outcomes_of(version_pk: int) -> dict[str, int]:
    """``{status_key: 1 if success else 0}`` for one child version's finished states.

    Read from the version's own report bindings, which is where the outcome was
    declared.  It is the only place it exists: a stamp records *that* a child was
    counted, never how it went.
    """
    from vinta_state_machines.batch_effects import REPORT_HANDLER
    from vinta_state_machines.models import StateMachineHook

    hooks = StateMachineHook.objects.filter(
        state_machine_version_id=version_pk,
        handler_key=REPORT_HANDLER,
        event=HookEvent.ENTER_STATE,
        is_active=True,
        state__isnull=False,
    ).select_related("state__status")
    return {
        hook.state.status.key: int(hook.params.get("outcome", SUCCESS) == SUCCESS)
        for hook in hooks
        if hook.state is not None
    }


def recount(batch: StatusBatch) -> tuple[int, int]:
    """Rebuild ``(finished, succeeded)`` from the stamps and the opaque reports.

    The authority the sweeper repairs a drifted counter against.  Both numbers are
    rebuilt together on purpose: repairing only ``finished`` would leave a batch reading
    as though a child had failed, and route the join down the wrong edge.
    """
    finished = 0
    succeeded = 0
    seen: dict[int, dict[str, int]] = {}

    for relation in batch_member_relations(type(batch)):
        model = relation.related_model
        stamp = get_batch_field_config(model, relation.field.name).reported_at_field
        rows = model._default_manager.filter(
            **{relation.field.name: batch.pk, f"{stamp}__isnull": False}
        )
        declared = status_fields_of(model)
        if not declared:
            # No governed status, so nothing to read an outcome from. It still counts
            # toward completeness; it just cannot count toward success.
            finished += rows.count()
            continue
        status_field = declared[0]
        config = get_status_field_config(model, status_field.name)
        version_column = f"{config.version_field}_id"
        grouped = rows.values(status_field.name, version_column).annotate(n=models.Count("pk"))
        for row in grouped:
            finished += row["n"]
            version_pk = row[version_column]
            if version_pk is None:
                continue
            if version_pk not in seen:
                seen[version_pk] = outcomes_of(version_pk)
            succeeded += row["n"] * seen[version_pk].get(row[status_field.name], 0)

    reports = _report_model().objects.filter(batch_id=batch.pk)
    finished += reports.count()
    succeeded += reports.filter(outcome=SUCCESS).count()
    return finished, succeeded
