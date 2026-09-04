"""Writing down what each side effect did: when it started, and how it ended.

The engine measures every handler it runs and hands the measurements to a
:class:`RunRecorder`, which decides -- from ``RECORD_SIDE_EFFECT_RUNS`` and the hook's
own ``record_runs`` -- which of them are worth a row.  A handler needs to know nothing
about any of this.

The awkward part is transactions, and it is worth stating plainly because the obvious
implementation gets it backwards.  Everything in
:func:`~vinta_state_machines.engine.transition` happens inside one ``atomic()`` block.
A handler that raises rolls that block back -- *including a row written to record the
failure*.  Write runs as they happen and you keep every success and lose every failure,
which is the opposite of useful.

So runs are buffered in memory and written once, after the atomic block has resolved,
on both paths: the successful one flushes with the history row it belongs to, and the
failing one flushes with ``status_transition=None`` and re-raises.  Buffering is also
what lets a ``before`` handler's run carry that history row at all -- it ran before the
row existed, but by flush time the row is there and it is the same move, so the link is
worth having.  Two more consequences worth knowing:

* One ``bulk_create`` per transition, not one ``INSERT`` per handler.
* A caller who wrapped ``transition()`` in a transaction of *their* own, and rolls it
  back, takes these rows with it -- the flush joins whatever transaction it finds.
  ``SIDE_EFFECT_RUN_SINK`` is the way out for a project that needs the record to
  survive that: it is handed the unsaved rows and can put them somewhere durable.

Deferred ``on_commit`` handlers are the exception to all of the above.  They run after
the commit, outside any transaction, and they are the ones that reach the network and
can hang -- so they are the one case where a half-written row is visible to anybody and
therefore worth writing.  Those get a row at ``started_at`` and an update when they
finish.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from vinta_state_machines.conf import get_setting
from vinta_state_machines.enums import RunRecording, SideEffectOutcome
from vinta_state_machines.side_effects import AbortTransition

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.db import models

    from vinta_state_machines.graph import HookSpec, TransitionSpec, VersionGraph
    from vinta_state_machines.models import SideEffectRun, StateMachineVersion, StatusTransition

logger = logging.getLogger(__name__)

__all__ = ["RunRecorder", "record_deferred_run"]


@dataclass(frozen=True)
class _Measurement:
    """One finished execution, before it is turned into a row.

    Kept as a plain object rather than an unsaved model instance so that the target's
    primary key can be read at flush time instead of at measure time: a ``before``
    handler on a record being created runs while ``instance.pk`` is still ``None``.
    """

    hook: HookSpec
    timing: str
    event: str
    started_at: Any
    completed_at: Any
    duration_ms: int
    outcome: str
    error_class: str = ""
    error_detail: str = ""


def _mode_for(hook: HookSpec) -> str:
    """How much to record for one binding: its own override, else the setting."""
    return str(hook.record_runs or get_setting("RECORD_SIDE_EFFECT_RUNS"))


def _describe_error(exc: BaseException) -> tuple[str, str]:
    """The exception's qualified class name, and its message if the project wants it."""
    cls = type(exc)
    name = f"{cls.__module__}.{cls.__qualname__}"[:200]
    if not get_setting("CAPTURE_SIDE_EFFECT_ERROR_DETAIL"):
        return name, ""
    return name, str(exc)[: get_setting("MAX_SIDE_EFFECT_ERROR_DETAIL")]


def _outcome_for(exc: BaseException) -> str:
    return (
        SideEffectOutcome.ABORTED if isinstance(exc, AbortTransition) else SideEffectOutcome.FAILED
    )


@dataclass
class RunRecorder:
    """Collects one transition's side-effect measurements and writes them once.

    Created per call to :func:`~vinta_state_machines.engine.transition`.  Held by the
    engine rather than by the context, so a handler cannot reach it and quietly change
    what gets recorded about itself.
    """

    instance: models.Model
    graph: VersionGraph
    version: StateMachineVersion
    spec: TransitionSpec
    from_key: str | None
    _measurements: list[_Measurement] = field(default_factory=list, repr=False)

    @property
    def empty(self) -> bool:
        return not self._measurements

    @contextmanager
    def measure(self, hook: HookSpec, timing: str, event: str) -> Iterator[None]:
        """Time one handler and keep the result if this binding asks to be recorded.

        Timing is a pair of ``time.monotonic()`` reads whatever the mode, because it
        costs nothing and deciding afterwards is what lets ``"failures"`` mode know how
        long the handler ran before it blew up.  ``"none"`` skips even that.
        """
        mode = _mode_for(hook)
        if mode == RunRecording.NONE:
            yield
            return

        started_at = timezone.now()
        start = time.monotonic()
        try:
            yield
        except BaseException as exc:
            error_class, error_detail = _describe_error(exc)
            self._measurements.append(
                _Measurement(
                    hook=hook,
                    timing=timing,
                    event=event,
                    started_at=started_at,
                    completed_at=timezone.now(),
                    duration_ms=_elapsed(start),
                    outcome=_outcome_for(exc),
                    error_class=error_class,
                    error_detail=error_detail,
                )
            )
            raise
        if mode == RunRecording.ALL:
            self._measurements.append(
                _Measurement(
                    hook=hook,
                    timing=timing,
                    event=event,
                    started_at=started_at,
                    completed_at=timezone.now(),
                    duration_ms=_elapsed(start),
                    outcome=SideEffectOutcome.SUCCEEDED,
                )
            )

    def flush(self, *, status_transition: StatusTransition | None = None) -> list[SideEffectRun]:
        """Write everything measured so far, and forget it.

        Called by the engine outside its atomic block, on the way out through either
        exit.  Safe to call with nothing buffered, which is the common case.
        """
        if not self._measurements:
            return []
        rows = [self.build_row(item, status_transition) for item in self._measurements]
        self._measurements.clear()
        _persist(rows)
        return rows

    def flush_after_failure(self) -> None:
        """Flush on the way out of a transition that is already raising.

        Best effort, and deliberately so: whatever the handler raised is what the
        caller needs to see, and a database that cannot take these rows must not be
        allowed to replace that error with its own.  The failure is logged instead.
        """
        try:
            self.flush(status_transition=None)
        except Exception:
            logger.exception(
                "Could not record side-effect runs for %s; the original error follows.",
                self.spec,
            )

    def build_row(
        self, item: _Measurement, status_transition: StatusTransition | None
    ) -> SideEffectRun:
        from django.contrib.contenttypes.models import ContentType

        from vinta_state_machines.models import SideEffectRun

        return SideEffectRun(
            hook_id=item.hook.pk,
            handler_key=item.hook.handler_key,
            status_transition=status_transition,
            target_type=ContentType.objects.get_for_model(self.instance, for_concrete_model=False),
            # Read now rather than when the handler ran: a ``before`` handler on a
            # record being created runs before it has a primary key to record.
            target_id=str(self.instance.pk or ""),
            state_machine_version=self.version,
            scope_id=self.graph.scope_pk,
            scope_key=self.graph.scope_key,
            status_field=self.graph.status_field,
            from_status_key=self.from_key or "",
            to_status_key=self.spec.to_key,
            action_key=self.spec.action,
            timing=item.timing,
            event=item.event,
            started_at=item.started_at,
            completed_at=item.completed_at,
            duration_ms=item.duration_ms,
            outcome=item.outcome,
            error_class=item.error_class,
            error_detail=item.error_detail,
        )


def _elapsed(start: float) -> int:
    return max(0, round((time.monotonic() - start) * 1000))


def _persist(rows: list[SideEffectRun]) -> None:
    """Hand the rows to the configured sink, or write them with the ORM."""
    from vinta_state_machines.models import SideEffectRun

    sink = get_setting("SIDE_EFFECT_RUN_SINK")
    if sink is not None:
        sink(rows)
        return
    SideEffectRun.objects.bulk_create(rows)


@contextmanager
def record_deferred_run(
    recorder: RunRecorder, hook: HookSpec, timing: str, event: str, record: Any
) -> Iterator[None]:
    """Record one ``on_commit`` handler, which runs outside the transaction.

    The only case that writes twice.  A deferred handler runs after the commit, on its
    own, and is exactly the kind that talks to something slow -- so a row saying it
    started is visible to anyone looking while it is still going, which is the whole
    point of storing ``started_at`` separately.

    A failure here is recorded and then re-raised.  It cannot roll the move back: the
    move committed before this ran, which is what ``on_commit`` means.
    """
    mode = _mode_for(hook)
    if mode == RunRecording.NONE:
        yield
        return

    started_at = timezone.now()
    start = time.monotonic()
    row: SideEffectRun | None = None
    if mode == RunRecording.ALL:
        row = recorder.build_row(
            _Measurement(
                hook=hook,
                timing=timing,
                event=event,
                started_at=started_at,
                completed_at=None,
                duration_ms=0,
                outcome=SideEffectOutcome.RUNNING,
            ),
            record,
        )
        row.duration_ms = None
        row.save()

    try:
        yield
    except BaseException as exc:
        error_class, error_detail = _describe_error(exc)
        _finish(
            recorder,
            row,
            hook=hook,
            timing=timing,
            event=event,
            record=record,
            started_at=started_at,
            start=start,
            outcome=_outcome_for(exc),
            error_class=error_class,
            error_detail=error_detail,
        )
        raise
    if mode == RunRecording.ALL:
        _finish(
            recorder,
            row,
            hook=hook,
            timing=timing,
            event=event,
            record=record,
            started_at=started_at,
            start=start,
            outcome=SideEffectOutcome.SUCCEEDED,
        )


def _finish(
    recorder: RunRecorder,
    row: SideEffectRun | None,
    *,
    hook: HookSpec,
    timing: str,
    event: str,
    record: Any,
    started_at: Any,
    start: float,
    outcome: str,
    error_class: str = "",
    error_detail: str = "",
) -> None:
    """Close out a deferred run: update the row it opened, or write one now.

    ``"failures"`` mode opened no row, so a failure writes its first and only one here.
    """
    duration_ms = _elapsed(start)
    if row is None:
        fresh = recorder.build_row(
            _Measurement(
                hook=hook,
                timing=timing,
                event=event,
                started_at=started_at,
                completed_at=timezone.now(),
                duration_ms=duration_ms,
                outcome=outcome,
                error_class=error_class,
                error_detail=error_detail,
            ),
            record,
        )
        _persist([fresh])
        return
    row.completed_at = timezone.now()
    row.duration_ms = duration_ms
    row.outcome = outcome
    row.error_class = error_class
    row.error_detail = error_detail
    row.save(
        update_fields=[
            "completed_at",
            "duration_ms",
            "outcome",
            "error_class",
            "error_detail",
            "modified_at",
        ]
    )
