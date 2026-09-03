"""Reading how far along a fan-out is, as plain data.

The library gives you the thing to call.  It does not give you the loop that calls it:
no polling, no interval setting, no shipped script.  How often anyone looks is the
project's business, the same way the queue is.

Everything here returns JSON-serialisable dicts, so a view is four lines::

    def import_progress(request, pk):
        record = get_object_or_404(Import, pk=pk)
        return JsonResponse(batch_tree(record))

Poll that every two seconds, push it down a channel, or never refresh at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vinta_state_machines.batches import batch_model
from vinta_state_machines.conf import get_setting

if TYPE_CHECKING:
    from django.db import models

    from vinta_state_machines.models import StatusBatch

__all__ = ["batch_tree", "current_batch", "progress_of", "tree_of"]


def current_batch(instance: models.Model, field_name: str = "status_key") -> StatusBatch | None:
    """The batch that matters for this record right now.

    The live one if there is one; otherwise the most recent, because "what happened to
    the last run" is the question somebody opening a finished import is asking.
    """
    model = batch_model()
    batches = model.objects.for_object(instance, status_field=field_name)
    found: StatusBatch | None = batches.live().first() or batches.first()
    return found


def progress_of(instance: models.Model, field_name: str = "status_key") -> dict[str, Any] | None:
    """How far along this record's fan-out is, or ``None`` if it never had one."""
    batch = current_batch(instance, field_name)
    return None if batch is None else describe(batch)


def batch_tree(instance: models.Model, field_name: str = "status_key") -> dict[str, Any] | None:
    """The whole nested run under this record, as one nested dict.

    A tree of **batches**, not of children: only batches carry ``parent_batch``, so a
    run over a million rows still draws as a handful of nodes.  Its size is bounded by
    ``MAX_BATCH_DEPTH`` and by how many nested batches exist, never by how much work
    they did.
    """
    root = current_batch(instance, field_name)
    return None if root is None else tree_of(root)


def tree_of(root: StatusBatch) -> dict[str, Any]:
    """``root`` and everything under it, walked one level at a time.

    Level by level rather than with a recursive CTE: depth is capped by
    ``MAX_BATCH_DEPTH``, so this is at most ten small queries and it works on every
    backend Django supports.
    """
    model = batch_model()
    nodes: dict[int, dict[str, Any]] = {root.pk: describe(root)}
    frontier = [root.pk]

    for _ in range(int(get_setting("MAX_BATCH_DEPTH")) + 1):
        if not frontier:
            break
        children = list(
            model.objects.filter(parent_batch_id__in=frontier).select_related("opened_in_status")
        )
        frontier = []
        for child in children:
            node = describe(child)
            nodes[child.pk] = node
            nodes[child.parent_batch_id]["children"].append(node)
            frontier.append(child.pk)

    return nodes[root.pk]


def describe(batch: StatusBatch) -> dict[str, Any]:
    """One batch as plain data. No model instances to leak into a template."""
    target = batch.target
    return {
        "id": batch.pk,
        "target": str(target) if target is not None else "",
        "target_type": batch.target_type.model,
        "target_id": batch.target_id,
        "status_field": batch.status_field,
        "opened_in_status": batch.opened_in_status.key,
        "lifecycle": batch.lifecycle,
        "sealed": batch.sealed,
        "total": batch.total,
        "finished": batch.finished,
        "succeeded": batch.succeeded,
        "failed": batch.failed,
        "progress": round(batch.progress, 4),
        "failure_reason": batch.failure_reason,
        "failure_detail": batch.failure_detail,
        "depth": batch.depth,
        "timeout_at": batch.timeout_at.isoformat() if batch.timeout_at else None,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "children": [],
    }
