# Proposal: global instrumentation

**Status:** implemented — all three phases, unreleased
**Affects:** `engine`, `side_effects`, `graph`, `batches`, `sweeper`, `services`, `conf`
**New module:** `vinta_state_machines/instruments.py`

## As built

Shipped as described, with four deltas worth knowing about:

- **`BatchEvent` counters follow the batch row.** `total` / `finished` / `succeeded`,
  not the `total` / `completed` / `failed` sketched below — a second vocabulary for the
  same three numbers would have been a needless translation. `sweep()` reports its own
  count as `swept` for the same reason: `total` already means "children a batch expects".
- **`TransitionEvent.to_status` and `.transition_name` start empty.** The span opens
  before an edge is chosen, which is what makes a refusal observable at all, so the
  engine fills them in through `span.set` once `_select` has run. `Span.data` merges the
  two, and every built-in instrument reads that rather than the raw event.
- **`AuthoringEvent` gained `target_label` / `target_id`.** `rebase_record` is the one
  authoring operation that moves a record, and leaving it unable to say which was worse
  than one more pair of optional fields.
- **`InspectionEvent` is a real event type**, not just a flag. It carries the operation,
  the record and — on `can_transition` — the action and the answer.

`SideEffectContext.span_id` is the only public API addition outside the new module.

## The problem

The library can already tell you a great deal about *one machine*: hooks fire around a
change, `SideEffectRun` records what each handler did, `StatusTransition` is an
append-only account of every move. All of it is authored per version and stored in the
database.

None of it answers the questions an operator asks:

- How long does `risk.assess` take at p95, across every tenant?
- Which side effect started timing out at 03:00?
- Which transitions are being **refused**, and why — guard, permission, approval?
- Is the batch sweeper re-dispatching more than it used to?
- When a request is slow in Datadog, which transition inside it was slow?

The last one is the tell. Those questions are not about a machine, they are about a
deployment: the answer has to be uniform across every version of every machine, has to
exist without anybody authoring a row, and has to leave the process — into logs, into an
APM, into a metrics pipeline — rather than into a table.

That is a different concern from a side effect, and it wants a different door.

## Why not the three obvious answers

**A hook wired to `any_transition`.** This is the closest thing that exists today, and it
fails on three counts. It needs a `StateMachineHook` row on every version of every machine,
so "instrument everything" becomes an authoring chore that a newly published version
silently forgets. It cannot see a move that did not happen — a guard that did not hold, a
permission that was missing, an approval that was required — because a refused transition
raises before any hook fires, and refusals are exactly what you want to alert on. And it
cannot bracket: a handler runs at a point in time, so measuring the whole move means two
hooks and a thread-local to pair them.

There is also a boundary worth keeping sharp. Hooks are *tenant data*, subject to
`ScopeCapabilityRule` and authored by the same people who draw the graph. Telemetry is
*deployment configuration*, owned by whoever runs the service. Mixing them means a tenant
can switch off your APM.

**Django signals.** Familiar, and third-party apps can attach without touching settings —
genuinely nice. But `Signal.send` propagates a receiver's exception straight into the
transition, so a broken metrics client rolls back a business operation; `send_robust`
avoids that by swallowing the error entirely, which is how a telemetry pipeline dies
quietly. Receivers are unordered and globally mutable. There is no bracketing, so every
duration needs a paired signal plus manual correlation. And `send` walks the receiver list
and does a sender lookup on every call, so "nothing is listening" is not free on a hot
path.

Signals are still worth having for people who want them — see `SignalInstrument` below.
They are just the wrong thing to build the mechanism *on*.

**Wrapping `transition()` in the project.** Works until something else calls it: the batch
join calls it, `run_batch_operation` calls it, a management command calls it. The wrapper
only sees the moves that went through the project's own service layer.

## The shape

One module, `vinta_state_machines.instruments`. A project registers *instruments* —
objects with a single method that brackets an observation:

```python
STATE_MACHINES = {
    "INSTRUMENTS": [
        "vinta_state_machines.instruments.LoggingInstrument",
        "myproject.telemetry.DatadogInstrument",
    ],
}
```

```python
class Instrument:
    """Something that watches. Every method defaults to doing nothing."""

    def observe(self, span: Span) -> AbstractContextManager[None]:
        return nullcontext()
```

The engine calls a dispatcher which composes every registered instrument onto one
`ExitStack`:

```python
with observe(TransitionEvent(...)) as span:
    ...  # the move
    span.set(record_pk=record.pk)
```

Three properties fall out of the context-manager shape, and they are why it is worth
preferring over a pair of callbacks:

- **Duration is free and correct**, including for a move that raised half way through.
- **Nesting works by construction.** A transition fired from inside a side effect of
  another transition produces a nested span, because the inner `with` opens inside the
  outer one. OpenTelemetry's context propagation does the parenting for you.
- **An APM integration is four lines**, because `tracer.start_as_current_span` is already
  a context manager.

`Span` is the mutable half — what we learn *while* it runs. The dispatcher builds one and
hands the same object to every instrument, so an instrument reads it on the way out:

```python
@dataclass
class Span:
    event: Event
    id: str  # opaque, per-observation
    parent_id: str | None  # the enclosing span, via contextvar
    attributes: dict[str, Any] = field(default_factory=dict)
    outcome: str = Outcome.OK  # ok | denied | failed | aborted
    error_class: str = ""  # qualified class name, never the message

    def set(self, **attrs: Any) -> None:
        self.attributes.update(attrs)
```

`outcome` is derived from what came through the block, not asserted by the caller:
`StateMachineError` → `denied`, `AbortTransition` → `aborted`, anything else → `failed`.
That classification is the single most useful thing this proposal adds, because "refused"
and "blew up" are the two failure modes that today look identical from outside.

## The events

Frozen dataclasses in `instruments.py`, one per family. An instrument dispatches on type
(`match` / `isinstance` / `singledispatchmethod`), so adding an event later does not break
an existing instrument.

```python
@dataclass(frozen=True)
class TransitionEvent:
    machine_key: str
    version_pk: int
    version_label: str
    scope_key: str  # "" is global
    entity_type: str
    status_field: str
    target_label: str  # "risks.risk"
    target_id: str  # primary key, as a string
    action: str
    transition_name: str
    from_status: str | None
    to_status: str
    actor_type: str  # user | system | service | ...
    actor_key: str  # the identity's portable key
```

```python
@dataclass(frozen=True)
class SideEffectEvent:
    handler_key: str
    hook_pk: int
    timing: str  # before | after
    event: str  # transition | enter_state | leave_state | ...
    deferred: bool  # ran in on_commit, outside the transaction
    transition: TransitionEvent
```

```python
@dataclass(frozen=True)
class BatchEvent:
    operation: str  # open | seal | report | join | close | cancel | abandon
    batch_pk: int
    machine_key: str
    scope_key: str
    join_action: str
    lifecycle: str
    total: int | None
    completed: int | None
    failed: int | None
    failure_reason: str  # "" unless it ended badly
```

```python
@dataclass(frozen=True)
class AuthoringEvent:
    operation: str  # publish | clone | archive | set_default | rebase | define
    machine_key: str
    version_pk: int | None
    version_label: str
    scope_key: str
    actor_key: str
```

```python
@dataclass(frozen=True)
class GraphLoadEvent:
    machine_key: str
    version_pk: int
    version_label: str
    states: int
    transitions: int
    hooks: int
```

Note what is **not** on any of them: no `comment`, no `metadata`, no exception message, no
model instance. See *What an event may carry*, below.

## Call sites

| Where | Span | Notes |
|---|---|---|
| `engine.transition()` | `TransitionEvent` | Opened before version resolution, so a refusal is inside the span. Wraps the atomic block and the run flush. |
| `side_effects.run_hooks()` | `SideEffectEvent` | Composed onto the same `with` as `RunRecorder.measure`, so there is one timing point, not two. |
| `side_effects._bind()` | `SideEffectEvent(deferred=True)` | The `on_commit` closure captures the parent span id at bind time — by the time it runs, the contextvar is long gone. |
| `batches.open_batch/seal/report/abandon` | `BatchEvent` | |
| `batches.run_batch_operation()` | `BatchEvent` | The worker entry point. Its inner `transition()` nests underneath. |
| `sweeper.sweep()` | `BatchEvent(operation="sweep")` | `span.set(**report.__dict__)` on the way out. |
| `services.publish_version` and friends | `AuthoringEvent` | Low frequency, audit-relevant, effectively free. |
| `graph.build_graph()` | `GraphLoadEvent` | Cache misses only. Answers "why is this request slow after a deploy". |

Deliberately **not** instrumented by default: `can_transition()` and
`available_transitions()`. They are called once per button per record per page render, and
a span each would swamp the signal. A `INSTRUMENT_INSPECTION` flag can turn them on for
somebody chasing an N+1.

`engine.transition()` also gains a `span_id` on `SideEffectContext`, so a handler's own log
lines correlate with the move that caused them without a thread-local of its own.

## Safety

Three constraints, in priority order.

### Telemetry must never break a move

Every instrument is entered and exited through a guard that swallows and logs *the
instrument's own* exceptions, while letting the body's exception through untouched:

```python
@contextmanager
def _guarded(instrument: Instrument, span: Span) -> Iterator[None]:
    if get_setting("INSTRUMENT_STRICT"):
        with instrument.observe(span):
            yield
        return
    try:
        cm = instrument.observe(span)
        cm.__enter__()
    except Exception:
        logger.exception("Instrument %r failed on enter; continuing.", instrument)
        yield  # the body still runs
        return
    try:
        yield
    except BaseException as exc:
        _safe_exit(cm, instrument, exc)
        raise  # the caller's error, unchanged
    else:
        _safe_exit(cm, instrument, None)
```

The distinction is the whole point: an APM client that cannot reach its collector must not
be able to turn a successful transition into a failed one, and must not be able to replace
a real error with its own. `INSTRUMENT_STRICT` (default `False`) makes it raise instead,
which is what you want in the test suite so a broken instrument fails loudly there.

### An event may only carry opaque identifiers

`transition(comment=...)` and `transition(metadata=...)` are free-form caller text. In a
system where this library governs clinical or financial records, that text is the most
likely place for regulated data to sit. Instruments send data *out of the process*, into
log aggregators and APM vendors that are usually outside whatever compliance boundary the
database sits in.

So the rule is: **an event carries keys, not contents.** Status keys, action keys, handler
keys, scope keys, primary keys, class names. Never a comment, never metadata, never a
model instance an instrument could walk, never an exception message.

This is the same stance `runs.py` already takes with `CAPTURE_SIDE_EFFECT_ERROR_DETAIL`
(off by default, because "an exception string routinely quotes the value that broke it"),
extended to a channel where the consequences are larger. Two escape hatches, both
opt-in and both narrow:

```python
"INSTRUMENT_METADATA_KEYS": (),      # allowlist; only these keys are copied
"INSTRUMENT_ERROR_DETAIL": False,    # attach the exception message, not just its class
```

An allowlist rather than a denylist, so adding a metadata key to a project's transitions
cannot leak by default.

### Nothing is registered must cost nothing

`get_setting("INSTRUMENTS")` resolves and caches the instrument tuple once, through the
existing `_IMPORT_STRINGS` machinery. The dispatcher short-circuits:

```python
def observe(event: Event) -> AbstractContextManager[Span]:
    instruments = get_setting("INSTRUMENTS")
    if not instruments:
        return _NULL_SPAN_CONTEXT  # a module-level singleton, no allocation
    return _observe(event, instruments)
```

The cost on the default path is a tuple truthiness check and a `with` on a pre-built
object. `Span.set()` on the null span is a no-op, so call sites need no branching of their
own.

## Built-in instruments

Shipped in `instruments.py`, all optional, none enabled by default.

**`LoggingInstrument`** — one structured line per observation, on dedicated logger names
(`vinta_state_machines.transition`, `.side_effect`, `.batch`, `.authoring`), with the event
fields in `extra=` rather than interpolated into the message. A project with a JSON
formatter or `structlog` gets queryable logs for free; a project with plain logging gets a
readable line and can silence a family with one `LOGGING` entry. Level is derived from
outcome: `ok` → `DEBUG`, `denied` → `INFO`, `failed`/`aborted` → `WARNING`.

**`MetricsInstrument`** — counters and timings against a small protocol
(`incr(name, tags)`, `timing(name, ms, tags)`) that statsd, `datadog.statsd` and
`prometheus_client` wrappers all satisfy with an adapter of a few lines. Ships with no
dependency and no client; the project supplies one. Tag cardinality is the trap here, so
the default tag set is `machine_key`, `action`, `outcome` — deliberately excluding
`target_id` and `scope_key`, with a setting to add them back for installations small
enough to afford it.

**`OpenTelemetryInstrument`** — under an `[otel]` extra. OTel is the one integration worth
shipping ourselves, because Datadog, Honeycomb, Grafana, New Relic and Sentry all ingest
it, so one instrument covers every APM anybody is likely to ask for. Attributes are
prefixed `state_machine.*`. `record_exception` defaults to `False`, since OTel's exception
recording captures the message and the stack trace — see the PHI constraint above.

**`SignalInstrument`** — re-emits observations as Django signals
(`transition_started` / `transition_finished`, etc.) for projects that would rather attach
receivers than write a class, and for third-party apps that want to observe without asking
the project to edit a setting. Because it is an instrument, a receiver that raises is
already isolated, and its cost is only paid by projects that opt in.

## How this relates to `SideEffectRun`

They are the same measurement, kept for different readers, and both should exist.

`SideEffectRun` is durable, queryable, joinable to the history row, and visible in the
admin. It answers "what happened to *this record*" months later, and it survives the APM's
retention window. Instruments are ephemeral and aggregate: they answer "what is happening
to *the system*" right now.

They will share a call site in `run_hooks`, and the timing already computed by
`RunRecorder.measure` is handed to the span rather than measured twice.

There is a tempting refactor — express run recording as a built-in `RunRecordingInstrument`
and delete `RunRecorder` — and I think we should **not** do it, at least not now. The
recorder's buffer-and-flush-outside-the-atomic-block dance exists because a handler that
raises rolls back the row recording its own failure. That is subtle, tested, and specific
to a database sink; folding it into a generic instrument interface would make the interface
worse to explain rather than better. Revisit if a second sink ever wants the same
treatment.

## Rollout

**Phase 1 — the mechanism.** ✅ `instruments.py` with `Instrument`, `Span`, `Event`, the
dispatcher and its guard; `TransitionEvent` and `SideEffectEvent`; call sites in
`engine.transition` and `side_effects.run_hooks`; `LoggingInstrument`; settings and docs.

**Phase 2 — the rest of the surface.** ✅ `BatchEvent`, `AuthoringEvent`, `GraphLoadEvent`,
`InspectionEvent` and their call sites. `MetricsInstrument`, `SignalInstrument`.

**Phase 3 — OpenTelemetry.** ✅ The `[otel]` extra and a README section. The SDK is a dev
dependency, so the shipped mapping is tested against a real tracer and an in-memory
exporter rather than a stand-in.

All of it is additive and backwards compatible: `INSTRUMENTS` defaults to `()`, a project
that sets nothing sees no behaviour change, and there are no migrations.
`SideEffectContext` gained one field, which is additive on a frozen dataclass with a
default.

## Open questions, as resolved

1. **Sampling — left out.** The instrument is the right place to decide, and OTel already
   samples. Sampling a refusal is usually the wrong trade, and nothing here forecloses
   adding `INSTRUMENT_SAMPLE_RATE` to the dispatcher later.
2. **Instantiation — accept anything.** A dotted path, a class, a factory or an instance.
   Anything callable is called and whatever comes back is used, so a no-argument
   instrument registers as a bare class and `MetricsInstrument`, which needs a client,
   registers through a one-line factory.
3. **Async — nothing done, nothing foreclosed.** The event dataclasses stay free of
   anything that would make an `aobserve()` twin hard, should `atransition()` ever land.
4. **`available_transitions` — off, but reachable.** `INSTRUMENT_INSPECTION` turns both it
   and `can_transition` on. It is the one pair of call sites that checks the setting
   *before* building an event, because they run once per button per record per page
   render.
