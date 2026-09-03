"""The sweeper: what keeps a fan-out from waiting forever.

Nothing here is clever.  It exists because processes die, queues drop things, and a
counter is only an approximation of the stamps.  Four passes, each one repairing a
different way a batch can get stuck:

1. **Repair** a counter that disagrees with the stamps.
2. **Claim** a batch that is complete but that nobody claimed -- a worker committed its
   count and was killed before it ran the claim.
3. **Re-dispatch** a batch that has sat in ``joining`` too long, which means the worker
   that claimed it never finished the job.
4. **Time out** a batch past its deadline, so the parent leaves the waiting state
   instead of sitting in it forever.

Run it about once a minute.  The library ships the command and no scheduler; cron,
Celery beat, a Kubernetes CronJob or a shell loop all do the job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db.models import DateTimeField, ExpressionWrapper, F, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from vinta_state_machines.batches import JOIN, batch_model, dispatch, recount, try_claim
from vinta_state_machines.conf import get_setting
from vinta_state_machines.enums import BatchFailureReason, BatchLifecycle

if TYPE_CHECKING:
    from datetime import datetime

__all__ = ["SweepReport", "sweep"]


@dataclass
class SweepReport:
    """What one pass actually did. Printed by the command, asserted on by tests."""

    repaired: int = 0
    claimed: int = 0
    redispatched: int = 0
    timed_out: int = 0

    @property
    def total(self) -> int:
        return self.repaired + self.claimed + self.redispatched + self.timed_out

    def __str__(self) -> str:
        return (
            f"repaired {self.repaired}, claimed {self.claimed}, "
            f"re-dispatched {self.redispatched}, timed out {self.timed_out}"
        )


def sweep(*, now: datetime | None = None, limit: int = 1000) -> SweepReport:
    """Run one pass. Safe to run concurrently with itself and with live workers.

    Every write it makes is the same conditional ``UPDATE`` the runtime uses, so two
    sweepers racing each other reach the same place as one.
    """
    moment = now or timezone.now()
    report = SweepReport()

    report.repaired = _repair_counters(limit)
    report.claimed = _claim_complete(limit)
    report.redispatched = _redispatch_stuck(moment, limit)
    report.timed_out = _time_out(moment, limit)
    return report


def _repair_counters(limit: int) -> int:
    """Bring drifted counters back to what the stamps say.

    The gap this closes: a worker stamps its child, then dies before incrementing.  The
    stamp is the truth, so the counter is what moves.
    """
    model = batch_model()
    repaired = 0
    for batch in model.objects.open()[:limit]:
        finished, succeeded = recount(batch)
        if (finished, succeeded) == (batch.finished, batch.succeeded):
            continue
        # Still conditional on `open`: a batch claimed while we were counting has had
        # its numbers read by the join already, and must not be rewritten under it.
        repaired += model.objects.filter(pk=batch.pk, lifecycle=BatchLifecycle.OPEN).update(
            finished=finished, succeeded=succeeded
        )
    return repaired


def _claim_complete(limit: int) -> int:
    """Claim batches that are complete but that nobody claimed.

    A worker can commit its count and be killed before running the claim.  Then
    ``finished`` equals ``total``, nothing is marked joining, and without this the batch
    waits forever.
    """
    model = batch_model()
    claimed = 0
    stuck = model.objects.open().complete().values_list("pk", flat=True)[:limit]
    for batch_id in list(stuck):
        if try_claim(batch_id):
            claimed += 1
    return claimed


def _redispatch_stuck(moment: datetime, limit: int) -> int:
    """Hand the join back to the dispatcher for batches whose worker never returned.

    ``joining_at`` rather than ``modified_at``: ``auto_now`` is applied by
    ``Model.save()`` and every write in this design is a queryset ``update()``, so
    ``modified_at`` would read the row's creation and re-dispatch everything on the
    first pass.
    """
    model = batch_model()
    default = get_setting("BATCH_JOIN_RETRY_AFTER")
    overdue = (
        model.objects.joining()
        .exclude(joining_at__isnull=True)
        .annotate(
            retry_at=ExpressionWrapper(
                F("joining_at") + Coalesce("join_retry_after", Value(default)),
                output_field=DateTimeField(),
            )
        )
        .filter(retry_at__lt=moment)
        .values_list("pk", flat=True)[:limit]
    )
    batch_ids = list(overdue)
    for batch_id in batch_ids:
        dispatch(JOIN, batch_id)
    return len(batch_ids)


def _time_out(moment: datetime, limit: int) -> int:
    """Claim batches past their deadline, so the parent stops waiting.

    A timeout ends a batch that is *not* complete, which is why it claims with a reason
    rather than through the ordinary path.  The parent still has to be moved out of the
    state it is waiting in; the graph decides where to, by reading the reason.
    """
    model = batch_model()
    expired = (
        model.objects.open()
        .exclude(timeout_at__isnull=True)
        .filter(timeout_at__lt=moment)
        .values_list("pk", flat=True)[:limit]
    )
    timed_out = 0
    for batch_id in list(expired):
        if try_claim(
            batch_id,
            failure_reason=BatchFailureReason.TIMEOUT,
            failure_detail="The batch passed its deadline with work still unfinished.",
        ):
            timed_out += 1
    return timed_out
