"""Global instrumentation: watching the deployment rather than the machine.

Hooks answer "what should happen when this record moves".  Instruments answer "what is
happening to this service" -- p95 of an action across every tenant, which transitions
are being *refused*, which side effect started timing out at 03:00.  Those are operator
questions, so they are configured by whoever runs the service::

    STATE_MACHINES = {
        "INSTRUMENTS": [
            "vinta_state_machines.instruments.LoggingInstrument",
            "myproject.telemetry.make_datadog_instrument",
        ],
    }

An instrument has one method, and it *brackets* the thing it watches::

    class Instrument:
        def observe(self, span: Span) -> AbstractContextManager[None]: ...

Three things follow from that shape, and they are why it is preferred over a pair of
callbacks or a signal:

* Durations are correct, including for a move that raised half way through.
* Nesting works by construction.  A transition fired from inside a side effect of
  another transition opens its span inside the outer one, so the tree is free -- no
  correlation id to thread through, and OpenTelemetry's own context propagation does
  the parenting.
* An APM integration is a handful of lines, because ``start_as_current_span`` is
  already a context manager.

Three constraints hold the design together, in priority order.

**Telemetry must never break a move.**  Every instrument is entered and exited through
:func:`_guarded`, which swallows and logs *the instrument's own* exceptions while
letting the caller's through untouched.  An APM client that cannot reach its collector
must not turn a successful transition into a failed one, and must not replace a real
error with its own.  ``INSTRUMENT_STRICT`` turns that off, which is what a test suite
wants so a broken instrument fails loudly there.

**An event carries keys, not contents.**  Status keys, action keys, handler keys, scope
keys, primary keys, class names.  Never a ``comment``, never ``metadata``, never a model
instance an instrument could walk, never an exception message.  Both of the first two
are free-form caller text, and instruments send what they are given *out of the
process*, to log aggregators and APM vendors that are usually outside whatever boundary
the database sits in.  This is the stance :mod:`vinta_state_machines.runs` already takes
with ``CAPTURE_SIDE_EFFECT_ERROR_DETAIL``, extended to a channel where the consequences
are larger.  ``INSTRUMENT_METADATA_KEYS`` and ``INSTRUMENT_ERROR_DETAIL`` are the two
ways out, both opt-in, and the first is an allowlist rather than a denylist so adding a
metadata key to a project's transitions cannot leak by default.

**Nothing registered must cost almost nothing.**  With no instruments configured,
:func:`observe` does a tuple truthiness check and yields a shared no-op span, and
:meth:`Span.set` on that span does nothing -- so a call site never branches on whether
anybody is watching.  What is *not* free is building the event itself, which happens
before :func:`observe` is called: one frozen dataclass per observation, which is
nothing against the several queries a transition already runs, and is why the one
genuinely hot pair of call sites -- ``available_transitions`` and ``can_transition``,
called once per button per record per page render -- checks ``INSTRUMENT_INSPECTION``
before it builds anything at all.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, ClassVar

from django.core.signals import setting_changed
from django.dispatch import Signal, receiver
from django.utils.module_loading import import_string

from vinta_state_machines.conf import get_setting
from vinta_state_machines.enums import IdentityType

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from contextlib import AbstractContextManager

logger = logging.getLogger(__name__)

__all__ = [
    "AuthoringEvent",
    "BatchEvent",
    "Event",
    "GraphLoadEvent",
    "InspectionEvent",
    "Instrument",
    "LoggingInstrument",
    "MetricsInstrument",
    "OpenTelemetryInstrument",
    "Outcome",
    "SideEffectEvent",
    "SignalInstrument",
    "Span",
    "TransitionEvent",
    "actor_descriptor",
    "current_span",
    "filtered_metadata",
    "get_instruments",
    "observation_finished",
    "observation_started",
    "observe",
]


class Outcome:
    """How an observation ended.

    Derived from what came through the block rather than asserted by the caller, which
    is what makes the distinction that matters: a move the graph *refused* and a move
    that *blew up* look identical from outside the process today.
    """

    OK = "ok"
    DENIED = "denied"
    """A :class:`~vinta_state_machines.exceptions.StateMachineError`: a guard that did
    not hold, a missing permission, a required approval, an edge that is not there."""
    ABORTED = "aborted"
    """A ``before`` handler vetoed the move with ``AbortTransition``."""
    FAILED = "failed"
    """Anything else: the handler, or the engine, actually broke."""


# ------------------------------------------------------------------------- events


@dataclass(frozen=True)
class Event:
    """What is being observed. Immutable, and carries identifiers only.

    Subclasses are matched on by instruments, so adding one later cannot break an
    instrument already in the field: anything it does not recognise falls through its
    dispatch and is ignored.
    """

    channel: ClassVar[str] = "event"
    """Names the family. Used for the logger name and the metric name."""

    @property
    def name(self) -> str:
        """The span name an APM should show."""
        return self.channel

    def attributes(self) -> dict[str, Any]:
        """This event flattened to a dict of scalars."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class TransitionEvent(Event):
    """One record moving, or being refused.

    ``to_status`` and ``transition_name`` are empty until the engine has chosen an edge,
    which happens after the span is already open -- opening it earlier is deliberate, so
    that a refusal is observed rather than missed.  The engine fills them in through
    :meth:`Span.set`, and :attr:`Span.data` merges the two.
    """

    channel: ClassVar[str] = "transition"

    machine_key: str
    version_pk: int
    version_label: str
    scope_key: str
    entity_type: str
    status_field: str
    target_label: str
    target_id: str
    action: str
    from_status: str | None
    actor_type: str
    actor_key: str
    to_status: str = ""
    transition_name: str = ""

    @property
    def name(self) -> str:
        return f"transition {self.action}"


@dataclass(frozen=True)
class SideEffectEvent(Event):
    """One registered handler running around a move."""

    channel: ClassVar[str] = "side_effect"

    handler_key: str
    hook_pk: int
    timing: str
    event: str
    deferred: bool
    """Ran in ``on_commit``, past the commit and outside the transaction."""
    transition: TransitionEvent

    @property
    def name(self) -> str:
        return f"side_effect {self.handler_key}"

    def attributes(self) -> dict[str, Any]:
        """Flattened, with the move's own attributes merged in rather than nested.

        An APM attribute is a scalar, so the transition this handler belongs to has to
        arrive as sibling keys rather than as a dict.
        """
        data = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "transition"}
        data.update(self.transition.attributes())
        return data


@dataclass(frozen=True)
class BatchEvent(Event):
    """One operation on a fan-out batch, or one pass of the sweeper.

    The counter names mirror the columns on the batch row (``total``, ``finished``,
    ``succeeded``) rather than inventing a second vocabulary for the same numbers.
    """

    channel: ClassVar[str] = "batch"

    operation: str
    """``open``, ``seal``, ``report``, ``join``, ``cancel``, ``abandon``, ``sweep``."""
    batch_pk: int | None = None
    machine_key: str = ""
    scope_key: str = ""
    join_action: str = ""
    lifecycle: str = ""
    depth: int | None = None
    total: int | None = None
    finished: int | None = None
    succeeded: int | None = None
    failure_reason: str = ""

    @property
    def name(self) -> str:
        return f"batch {self.operation}"


@dataclass(frozen=True)
class AuthoringEvent(Event):
    """A change to the catalog itself: publishing, cloning, archiving, rebasing."""

    channel: ClassVar[str] = "authoring"

    operation: str
    """``publish``, ``clone``, ``archive``, ``set_default``, ``rebase``, ``define``."""
    machine_key: str = ""
    version_pk: int | None = None
    version_label: str = ""
    scope_key: str = ""
    actor_type: str = ""
    actor_key: str = ""
    target_label: str = ""
    """Only ``rebase`` names a record: it is the one authoring operation that moves one."""
    target_id: str = ""

    @property
    def name(self) -> str:
        return f"authoring {self.operation}"


@dataclass(frozen=True)
class GraphLoadEvent(Event):
    """A version's graph being read out of the database and frozen.

    Only a cache miss gets here, which is what makes it worth watching: it answers "why
    did latency jump right after the deploy".
    """

    channel: ClassVar[str] = "graph"

    machine_key: str
    version_pk: int
    version_label: str
    states: int = 0
    transitions: int = 0
    hooks: int = 0

    @property
    def name(self) -> str:
        return f"graph_load {self.machine_key}@{self.version_label}"


@dataclass(frozen=True)
class InspectionEvent(Event):
    """A read-only question about what a record may do.

    Off unless ``INSTRUMENT_INSPECTION`` is on.  These are called once per button per
    record per page render, so a span each would swamp everything else -- but they are
    the first thing you want when a list view has gone quadratic.
    """

    channel: ClassVar[str] = "inspection"

    operation: str
    """``available_transitions`` or ``can_transition``."""
    machine_key: str
    version_pk: int
    version_label: str
    scope_key: str
    target_label: str
    target_id: str
    status_key: str
    action: str = ""

    @property
    def name(self) -> str:
        return f"inspect {self.operation}"


# --------------------------------------------------------------------------- span


@dataclass
class Span:
    """The mutable half of an observation: what we learn while it runs.

    One is built per observation and handed to *every* instrument, so an instrument
    reads it on the way out and sees whatever the engine learned in the meantime.
    """

    event: Event
    id: str = ""
    parent_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    outcome: str = Outcome.OK
    error_class: str = ""
    """Qualified class name of whatever came through. Never the message."""
    error_detail: str = ""
    """The message, and only when ``INSTRUMENT_ERROR_DETAIL`` asks for it."""
    duration_ms: int = 0

    def set(self, **attrs: Any) -> None:
        """Record something learned after the span was opened."""
        self.attributes.update(attrs)

    @property
    def data(self) -> dict[str, Any]:
        """The event's attributes, with anything :meth:`set` learned layered over."""
        merged = self.event.attributes()
        merged.update(self.attributes)
        return merged

    @property
    def failed(self) -> bool:
        return self.outcome != Outcome.OK


class _NullSpan(Span):
    """The span handed out when nothing is watching. Every write is dropped.

    Shared process-wide and therefore never mutated, which is why :meth:`set` is a
    no-op rather than a write nobody reads.
    """

    def set(self, **attrs: Any) -> None:
        return None


NULL_SPAN = _NullSpan(event=Event())


# --------------------------------------------------------------------- instruments


class Instrument:
    """Something that watches. Every method defaults to doing nothing.

    Subclass, override :meth:`observe`, and register the class -- or a factory that
    returns a configured instance -- in ``STATE_MACHINES["INSTRUMENTS"]``.
    """

    def observe(self, span: Span) -> AbstractContextManager[None]:
        """Bracket one observation. Read ``span`` on the way out, not on the way in."""
        return nullcontext()


_instruments: tuple[Instrument, ...] | None = None


def get_instruments() -> tuple[Instrument, ...]:
    """The configured instruments, resolved and instantiated once.

    An entry is a dotted path, a class, a factory or an instance.  Anything callable is
    called and whatever comes back is used, which lets a project register a bare class
    for the instruments that need no arguments and a factory for the ones that do::

        def make_datadog_instrument():
            return MetricsInstrument(client=statsd)
    """
    global _instruments
    if _instruments is None:
        _instruments = tuple(_build(entry) for entry in get_setting("INSTRUMENTS") or ())
    return _instruments


def _build(entry: Any) -> Instrument:
    target = import_string(entry) if isinstance(entry, str) else entry
    # A class or a factory is called; an already-built instance is used as it is.
    return target() if callable(target) else target  # type: ignore[no-any-return]


@receiver(setting_changed)
def _reset_instruments(*, setting: str, **kwargs: Any) -> None:
    global _instruments
    if setting == "STATE_MACHINES":
        _instruments = None


# ---------------------------------------------------------------------- observing

_current: ContextVar[Span | None] = ContextVar("vinta_state_machines_span", default=None)


def current_span() -> Span | None:
    """The span currently open on this thread or task, if any."""
    return _current.get()


@contextmanager
def observe(event: Event, *, parent_id: str | None = None) -> Iterator[Span]:
    """Watch one thing happen, and tell every instrument about it.

    ``parent_id`` overrides the ambient parent, which is what a deferred ``on_commit``
    handler needs: it runs after the transition's ``with`` block has closed, so the
    contextvar no longer holds the move it belongs to.  The caller captures the id at
    bind time and passes it here.
    """
    instruments = get_instruments()
    if not instruments:
        yield NULL_SPAN
        return

    parent = _current.get()
    span = Span(
        event=event,
        id=uuid.uuid4().hex,
        parent_id=parent_id if parent_id is not None else (parent.id if parent else None),
    )
    token = _current.set(span)
    start = time.monotonic()
    try:
        with ExitStack() as stack:
            for instrument in instruments:
                stack.enter_context(_guarded(instrument, span))
            try:
                yield span
            except BaseException as exc:
                span.outcome, span.error_class, span.error_detail = _classify(exc)
                raise
            finally:
                # Set before the stack unwinds, so every instrument's exit sees it.
                span.duration_ms = max(0, round((time.monotonic() - start) * 1000))
    finally:
        _current.reset(token)


@contextmanager
def _guarded(instrument: Instrument, span: Span) -> Iterator[None]:
    """Run one instrument so that it cannot affect what it is watching.

    Its own exceptions are logged and dropped; the body's are re-raised untouched.  The
    two are deliberately not symmetrical, and the asymmetry is the point of the module:
    a metrics client that cannot reach its collector must not roll back a business
    operation, and must not replace a real error with its own.
    """
    if get_setting("INSTRUMENT_STRICT"):
        with instrument.observe(span):
            yield
        return

    try:
        manager = instrument.observe(span)
        manager.__enter__()
    except Exception:
        logger.exception("Instrument %r failed on enter; continuing without it.", instrument)
        yield  # the body still runs
        return

    try:
        yield
    except BaseException as exc:
        _safe_exit(manager, instrument, exc)
        raise  # the caller's error, unchanged
    else:
        _safe_exit(manager, instrument, None)


def _safe_exit(
    manager: AbstractContextManager[Any], instrument: Instrument, exc: BaseException | None
) -> None:
    try:
        if exc is None:
            manager.__exit__(None, None, None)
        else:
            # A truthy return would suppress the caller's exception. An instrument does
            # not get to do that, so the answer is deliberately discarded.
            manager.__exit__(type(exc), exc, exc.__traceback__)
    except Exception:
        logger.exception("Instrument %r failed on exit; continuing.", instrument)


def _classify(exc: BaseException) -> tuple[str, str, str]:
    """Outcome, error class and -- only if asked for -- the message."""
    from vinta_state_machines.exceptions import StateMachineError
    from vinta_state_machines.side_effects import AbortTransition

    if isinstance(exc, AbortTransition):
        outcome = Outcome.ABORTED
    elif isinstance(exc, StateMachineError):
        outcome = Outcome.DENIED
    else:
        outcome = Outcome.FAILED

    cls = type(exc)
    error_class = f"{cls.__module__}.{cls.__qualname__}"[:200]
    detail = ""
    if get_setting("INSTRUMENT_ERROR_DETAIL"):
        detail = str(exc)[: get_setting("MAX_INSTRUMENT_ERROR_DETAIL")]
    return outcome, error_class, detail


# ------------------------------------------------------------------------- helpers


def filtered_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """The allowlisted keys of a caller's ``metadata``, and nothing else.

    An allowlist rather than a denylist: adding a key to a project's transitions must
    not be able to leak it by default.  Empty unless ``INSTRUMENT_METADATA_KEYS`` names
    something.
    """
    keys = get_setting("INSTRUMENT_METADATA_KEYS")
    if not keys or not metadata:
        return {}
    return {key: metadata[key] for key in keys if key in metadata}


def actor_descriptor(actor: Any) -> tuple[str, str]:
    """Name the acting principal as ``(type, key)``, without touching the database.

    Deliberately *not* :func:`~vinta_state_machines.identities.resolve_identity`: that
    writes a row, and a span must not add a query to the move it is measuring.  It also
    has to answer for a refused move, where no identity is ever recorded.
    """
    if actor is None:
        return (IdentityType.SYSTEM.value, "")
    identity_type = getattr(actor, "identity_type", None)
    if identity_type:
        return (str(identity_type), str(getattr(actor, "identity_key", "") or ""))
    pk = getattr(actor, "pk", None)
    if pk is not None:
        return (IdentityType.USER.value, str(pk))
    return (IdentityType.SYSTEM.value, "")


# ---------------------------------------------------------------- built-in: logging


class LoggingInstrument(Instrument):
    """One structured line per observation, on a logger named for the family.

    Loggers are ``vinta_state_machines.transition``, ``.side_effect``, ``.batch``,
    ``.authoring``, ``.graph`` and ``.inspection``, so a project silences a family with
    one ``LOGGING`` entry.  Everything the event knows goes into ``extra`` under a
    single ``state_machine`` key rather than as top level keys, because a bare ``extra``
    collides with :class:`logging.LogRecord`'s own attributes -- ``name``, ``module``,
    ``args`` -- and a collision raises inside the logging call.

    Level follows outcome, so the default configuration is quiet when things work and
    audible when they do not.  Override :meth:`emit` for a different payload shape.
    """

    prefix = "vinta_state_machines"

    LEVELS: ClassVar[dict[str, int]] = {
        Outcome.OK: logging.DEBUG,
        Outcome.DENIED: logging.INFO,
        Outcome.ABORTED: logging.WARNING,
        Outcome.FAILED: logging.WARNING,
    }

    @contextmanager
    def observe(self, span: Span) -> Iterator[None]:
        try:
            yield
        finally:
            self.emit(span)

    def emit(self, span: Span) -> None:
        log = logging.getLogger(f"{self.prefix}.{span.event.channel}")
        level = self.LEVELS.get(span.outcome, logging.INFO)
        if not log.isEnabledFor(level):
            return
        log.log(
            level,
            "%s %s in %dms",
            span.event.name,
            span.outcome,
            span.duration_ms,
            extra={"state_machine": self.payload(span)},
        )

    def payload(self, span: Span) -> dict[str, Any]:
        return {
            "span_id": span.id,
            "parent_span_id": span.parent_id,
            "channel": span.event.channel,
            "outcome": span.outcome,
            "duration_ms": span.duration_ms,
            "error_class": span.error_class,
            "error_detail": span.error_detail,
            **span.data,
        }


# ---------------------------------------------------------------- built-in: metrics


class MetricsInstrument(Instrument):
    """A counter and a timing per observation, against a statsd-shaped client.

    The client needs two methods -- ``incr(name, tags)`` and ``timing(name, ms, tags)``
    -- which statsd, ``datadog.statsd`` and a small ``prometheus_client`` wrapper all
    satisfy with a few lines of adapter.  No client ships with this, so register it
    through a factory::

        # myproject/telemetry.py
        def make_metrics_instrument():
            return MetricsInstrument(client=statsd)

    Cardinality is the trap here, and it is the reason the default tag set is three
    low-cardinality keys.  ``target_id`` and ``scope_key`` are available and deliberately
    not on by default: one series per record is how a metrics bill gets interesting.
    """

    default_tags: ClassVar[tuple[str, ...]] = ("machine_key", "action", "outcome")
    prefix = "state_machine"

    def __init__(
        self,
        client: Any,
        *,
        tags: tuple[str, ...] | None = None,
        prefix: str | None = None,
    ) -> None:
        self.client = client
        self.tags = tuple(tags) if tags is not None else self.default_tags
        if prefix is not None:
            self.prefix = prefix

    @contextmanager
    def observe(self, span: Span) -> Iterator[None]:
        try:
            yield
        finally:
            self.emit(span)

    def emit(self, span: Span) -> None:
        name = f"{self.prefix}.{span.event.channel}"
        tags = self.tags_for(span)
        self.client.incr(name, tags)
        self.client.timing(f"{name}.duration", span.duration_ms, tags)

    def tags_for(self, span: Span) -> dict[str, str]:
        """Only the configured keys, stringified. A key this event lacks becomes ``""``."""
        data = {**span.data, "outcome": span.outcome}
        return {key: str(data.get(key, "") or "") for key in self.tags}


# ---------------------------------------------------------------- built-in: signals

observation_started = Signal()
"""Sent as an observation opens. ``sender`` is the event class, ``span`` the span."""

observation_finished = Signal()
"""Sent as an observation closes, with the outcome and duration already on the span."""


class SignalInstrument(Instrument):
    """Re-emit observations as Django signals.

    For projects that would rather attach a receiver than write a class, and for a third
    party app that wants to observe without asking the project to edit a setting.
    ``sender`` is the event's class, so a receiver filters a family the ordinary way::

        @receiver(observation_finished, sender=TransitionEvent)
        def watch(sender, span, **kwargs): ...

    ``send_robust`` is used so one broken receiver does not stop the others -- and each
    failure is then logged here, rather than being returned to nobody, which is the flaw
    that makes ``send_robust`` a poor foundation on its own.
    """

    @contextmanager
    def observe(self, span: Span) -> Iterator[None]:
        self._send(observation_started, span)
        try:
            yield
        finally:
            self._send(observation_finished, span)

    def _send(self, signal: Signal, span: Span) -> None:
        for receiver_, response in signal.send_robust(sender=type(span.event), span=span):
            if isinstance(response, Exception):
                logger.error(
                    "Receiver %r raised while observing %s.",
                    receiver_,
                    span.event.name,
                    exc_info=response,
                )


# ---------------------------------------------------------- built-in: opentelemetry


class OpenTelemetryInstrument(Instrument):
    """Emit observations as OpenTelemetry spans.

    The one integration worth shipping here, because Datadog, Honeycomb, Grafana, New
    Relic and Sentry all ingest OTel: one instrument covers every APM anybody is likely
    to ask for.  Install with the extra::

        uv add 'vinta-django-state-machines[otel]'

    Exception recording is **off** by default, and both switches that would turn it back
    on are explicit.  OTel's own exception capture stores the message and the stack
    trace, and an exception message routinely quotes the value that broke it -- see this
    module's docstring on why that must not leave the process by accident.  The span
    still carries the exception's *class* and a status of ``ERROR``.
    """

    prefix = "state_machine"

    def __init__(
        self,
        tracer: Any = None,
        *,
        record_exception: bool = False,
        prefix: str | None = None,
    ) -> None:
        self._trace = _import_otel()
        self.tracer = tracer if tracer is not None else self._trace.get_tracer(__name__)
        self.record_exception = record_exception
        if prefix is not None:
            self.prefix = prefix

    @contextmanager
    def observe(self, span: Span) -> Iterator[None]:
        with self.tracer.start_as_current_span(
            span.event.name,
            record_exception=self.record_exception,
            # We set the status ourselves, from the exception's class rather than from
            # its message, which is what OTel would otherwise put in the description.
            set_status_on_exception=False,
        ) as otel_span:
            try:
                yield
            finally:
                if otel_span.is_recording():
                    otel_span.set_attributes(self.attributes_for(span))
                    otel_span.set_status(self.status_for(span))

    def attributes_for(self, span: Span) -> dict[str, Any]:
        """The span's data, prefixed and coerced to what OTel accepts."""
        attributes: dict[str, Any] = {}
        for key, value in span.data.items():
            if value is None:
                continue
            attributes[f"{self.prefix}.{key}"] = (
                value if isinstance(value, (str, bool, int, float)) else str(value)
            )
        attributes[f"{self.prefix}.outcome"] = span.outcome
        attributes[f"{self.prefix}.duration_ms"] = span.duration_ms
        if span.id:
            attributes[f"{self.prefix}.span_id"] = span.id
        if span.error_class:
            attributes[f"{self.prefix}.error_class"] = span.error_class
        if span.error_detail:
            attributes[f"{self.prefix}.error_detail"] = span.error_detail
        return attributes

    def status_for(self, span: Span) -> Any:
        status, code = self._trace.Status, self._trace.StatusCode
        if not span.failed:
            return status(code.OK)
        return status(code.ERROR, description=span.error_class or span.outcome)


def _import_otel() -> Any:
    try:
        from opentelemetry import trace
    except ImportError as exc:  # pragma: no cover - depends on what is installed
        raise ImportError(
            "OpenTelemetryInstrument needs the OpenTelemetry API. Install it with "
            "`uv add 'vinta-django-state-machines[otel]'`, or "
            "`pip install 'vinta-django-state-machines[otel]'`."
        ) from exc
    return trace
