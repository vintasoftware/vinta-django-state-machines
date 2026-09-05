"""Instruments: what gets observed, and what an instrument is not allowed to break."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

import pytest
from django.dispatch import receiver
from django.test import override_settings

from tests.testapp.models import Risk
from vinta_state_machines.engine import available_transitions, can_transition, transition
from vinta_state_machines.exceptions import (
    GuardFailed,
    InvalidVersionState,
    PermissionDenied,
    TransitionNotAllowed,
)
from vinta_state_machines.instruments import (
    AuthoringEvent,
    BatchEvent,
    Event,
    GraphLoadEvent,
    InspectionEvent,
    Instrument,
    LoggingInstrument,
    MetricsInstrument,
    Outcome,
    SideEffectEvent,
    SignalInstrument,
    Span,
    TransitionEvent,
    observation_finished,
    observe,
)
from vinta_state_machines.models import StateMachineHook
from vinta_state_machines.side_effects import AbortTransition

pytestmark = pytest.mark.django_db


# ------------------------------------------------------------------ test scaffolding


class Recorder(Instrument):
    """Keeps every span it is given, so a test can look at them afterwards."""

    def __init__(self) -> None:
        self.entered: list[Span] = []
        self.spans: list[Span] = []

    @contextmanager
    def observe(self, span: Span) -> Any:
        self.entered.append(span)
        try:
            yield
        finally:
            self.spans.append(span)

    def of(self, kind: type[Event]) -> list[Span]:
        return [span for span in self.spans if isinstance(span.event, kind)]

    def one(self, kind: type[Event]) -> Span:
        found = self.of(kind)
        assert len(found) == 1, f"expected exactly one {kind.__name__}, got {len(found)}"
        return found[0]


@contextmanager
def instrumented(*extra: Any, **overrides: Any) -> Any:
    """Install a :class:`Recorder`, plus anything else the test wants watching."""
    recorder = Recorder()
    with override_settings(
        STATE_MACHINES={
            "CACHE_GRAPHS": False,
            "INSTRUMENTS": [recorder, *extra],
            **overrides,
        }
    ):
        yield recorder


def _an_event(**overrides):
    """A minimal, valid transition event, for testing the dispatcher on its own."""
    values = {
        "machine_key": "risk.status",
        "version_pk": 1,
        "version_label": "1",
        "scope_key": "",
        "entity_type": "risk",
        "status_field": "status",
        "target_label": "testapp.risk",
        "target_id": "1",
        "action": "risk.assess",
        "from_status": "draft",
        "actor_type": "system",
        "actor_key": "",
    }
    return TransitionEvent(**{**values, **overrides})


def hook(version, handler="testapp.record", **kwargs):
    state_key = kwargs.pop("state", None)
    edge = kwargs.pop("transition", None)
    if state_key:
        kwargs["state"] = version.states.get(status__key=state_key)
    if edge:
        kwargs["transition"] = version.transitions.get(name=edge)
    return StateMachineHook.objects.create(
        state_machine_version=version, handler_key=handler, **kwargs
    )


# --------------------------------------------------------------------- the default


def test_nothing_is_watching_by_default(risk_version, risk):
    """The whole point of the null path: no configuration, no observation, no cost."""
    from vinta_state_machines.instruments import get_instruments

    assert get_instruments() == ()
    risk.transition("risk.assess")
    assert risk.status_key == "assessed"


def test_the_null_span_swallows_writes_without_sharing_them(risk_version, risk):
    from vinta_state_machines.instruments import NULL_SPAN

    with observe(_an_event()) as span:
        span.set(leaked="nope")

    assert span is NULL_SPAN
    assert NULL_SPAN.attributes == {}


# ------------------------------------------------------------------- a move observed


def test_a_move_is_observed_end_to_end(risk_version, risk, user):
    with instrumented() as recorder:
        risk.transition("risk.assess", actor=user)

    span = recorder.one(TransitionEvent)
    assert span.outcome == Outcome.OK
    assert span.data["machine_key"] == "risk.status"
    assert span.data["action"] == "risk.assess"
    assert span.data["from_status"] == "draft"
    assert span.data["to_status"] == "assessed"
    assert span.data["transition_name"] == "assess"
    assert span.data["target_label"] == "testapp.risk"
    assert span.data["target_id"] == str(risk.pk)
    assert span.data["actor_type"] == "user"
    assert span.data["actor_key"] == str(user.pk)
    assert span.data["record_pk"] is not None


def test_the_span_names_itself_after_the_action(risk_version, risk):
    with instrumented() as recorder:
        risk.transition("risk.assess")

    assert recorder.one(TransitionEvent).event.name == "transition risk.assess"


def test_the_system_is_named_when_nobody_acted(risk_version, risk):
    with instrumented() as recorder:
        risk.transition("risk.assess")

    assert recorder.one(TransitionEvent).data["actor_type"] == "system"


# --------------------------------------------------------------- refused vs. broken


def test_a_guard_that_does_not_hold_is_denied_not_failed(risk_version, risk):
    """The distinction the module exists for: refused and broken must not look alike."""
    risk.transition("risk.assess")
    risk.amount = 5000
    risk.save()

    with instrumented() as recorder, pytest.raises(GuardFailed):
        risk.transition("risk.mitigate")

    span = recorder.of(TransitionEvent)[-1]
    assert span.outcome == Outcome.DENIED
    assert span.error_class.endswith("GuardFailed")


def test_a_missing_permission_is_denied(risk_version, risk, user):
    risk.transition("risk.assess")

    with instrumented() as recorder, pytest.raises(PermissionDenied):
        risk.transition("risk.reject", actor=user)

    assert recorder.of(TransitionEvent)[-1].outcome == Outcome.DENIED


def test_an_edge_that_is_not_there_is_denied(risk_version, risk):
    with instrumented() as recorder, pytest.raises(TransitionNotAllowed):
        risk.transition("risk.mitigate")

    span = recorder.one(TransitionEvent)
    assert span.outcome == Outcome.DENIED
    assert span.error_class.endswith("TransitionNotAllowed")
    # Refused before an edge was chosen, so there is no destination to report.
    assert span.data["to_status"] == ""


def test_a_veto_is_aborted(risk_version, risk):
    hook(risk_version, handler="testapp.veto", timing="before", transition="assess")

    with instrumented() as recorder, pytest.raises(AbortTransition):
        risk.transition("risk.assess")

    assert recorder.one(TransitionEvent).outcome == Outcome.ABORTED


def test_a_handler_that_breaks_is_failed(risk_version, risk):
    hook(risk_version, handler="testapp.boom", timing="after", transition="assess")

    with instrumented() as recorder, pytest.raises(RuntimeError):
        risk.transition("risk.assess")

    span = recorder.one(TransitionEvent)
    assert span.outcome == Outcome.FAILED
    assert span.error_class == "builtins.RuntimeError"


def test_an_unpublished_version_is_denied(risk_draft):
    record = Risk.objects.create(title="early", status_machine_version=risk_draft)

    with instrumented() as recorder, pytest.raises(InvalidVersionState):
        transition(record, "risk.assess")

    span = recorder.of(TransitionEvent)[-1]
    assert span.outcome == Outcome.DENIED
    assert span.error_class.endswith("InvalidVersionState")


# ------------------------------------------------------------------- side effects


def test_a_handler_gets_its_own_span_under_the_move(risk_version, risk):
    hook(risk_version, timing="after", transition="assess")

    with instrumented() as recorder:
        risk.transition("risk.assess")

    move = recorder.one(TransitionEvent)
    handler = recorder.one(SideEffectEvent)
    assert handler.parent_id == move.id
    assert handler.data["handler_key"] == "testapp.record"
    assert handler.data["timing"] == "after"
    assert handler.data["deferred"] is False
    # The move's own attributes are flattened onto the handler's span, not nested.
    assert handler.data["action"] == "risk.assess"
    assert handler.data["to_status"] == "assessed"


def test_a_handler_that_breaks_is_failed_on_its_own_span(risk_version, risk):
    hook(risk_version, handler="testapp.boom", timing="after", transition="assess")

    with instrumented() as recorder, pytest.raises(RuntimeError):
        risk.transition("risk.assess")

    assert recorder.one(SideEffectEvent).outcome == Outcome.FAILED


def test_a_deferred_handler_keeps_the_move_as_its_parent(
    risk_version, risk, django_capture_on_commit_callbacks
):
    """The one case the ambient parent cannot supply: it runs past the ``with`` block."""
    hook(risk_version, timing="after", event="any_transition", on_commit=True)

    with instrumented() as recorder, django_capture_on_commit_callbacks(execute=True):
        risk.transition("risk.assess")

    move = recorder.one(TransitionEvent)
    handler = recorder.one(SideEffectEvent)
    assert handler.data["deferred"] is True
    assert handler.parent_id == move.id


def test_a_handler_can_read_the_span_it_runs_inside(risk_version, risk):
    seen: list[str] = []

    from vinta_state_machines.side_effects import register_side_effect

    @register_side_effect("tests.capture_span", replace=True)
    def capture(context: Any) -> None:
        seen.append(context.span_id)

    hook(risk_version, handler="tests.capture_span", timing="after", transition="assess")

    with instrumented() as recorder:
        risk.transition("risk.assess")

    assert seen == [recorder.one(TransitionEvent).id]


def test_span_id_is_empty_when_nothing_is_watching(risk_version, risk):
    seen: list[str] = []

    from vinta_state_machines.side_effects import register_side_effect

    @register_side_effect("tests.capture_span_off", replace=True)
    def capture(context: Any) -> None:
        seen.append(context.span_id)

    hook(risk_version, handler="tests.capture_span_off", timing="after", transition="assess")
    risk.transition("risk.assess")

    assert seen == [""]


# ------------------------------------------------------------------------ nesting


def test_a_move_fired_from_a_handler_nests_under_the_outer_one(risk_version, risk):
    """Free parenting: the inner ``with`` opens inside the outer one.

    The chain this asserts is move -> handler -> move -> handler, which is exactly what
    an APM has to draw for a fan-out to be readable.
    """
    from vinta_state_machines.side_effects import register_side_effect

    other = Risk.objects.create(title="second", amount=10)

    @register_side_effect("tests.move_another", replace=True)
    def move_another(context: Any) -> None:
        # The same hook fires for the inner move; without this it would recurse.
        if context.instance.pk != other.pk:
            transition(other, "risk.assess")

    hook(risk_version, handler="tests.move_another", timing="after", transition="assess")

    with instrumented() as recorder:
        risk.transition("risk.assess")

    moves = {span.data["target_id"]: span for span in recorder.of(TransitionEvent)}
    handlers = {span.data["target_id"]: span for span in recorder.of(SideEffectEvent)}

    outer, inner = moves[str(risk.pk)], moves[str(other.pk)]
    assert outer.parent_id is None
    assert handlers[str(risk.pk)].parent_id == outer.id
    assert inner.parent_id == handlers[str(risk.pk)].id
    assert handlers[str(other.pk)].parent_id == inner.id


# ------------------------------------------------ an instrument cannot break a move


class BreaksOnEnter(Instrument):
    def observe(self, span: Span) -> Any:
        raise RuntimeError("collector unreachable")


class BreaksOnExit(Instrument):
    @contextmanager
    def observe(self, span: Span) -> Any:
        yield
        raise RuntimeError("flush failed")


class Swallows(Instrument):
    """Tries to suppress whatever passed through it. Must not be allowed to."""

    class _Manager:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *exc_info: Any) -> bool:
            return True

    def observe(self, span: Span) -> Any:
        return self._Manager()


def test_an_instrument_that_breaks_on_enter_does_not_break_the_move(risk_version, risk, caplog):
    with instrumented(BreaksOnEnter()) as recorder, caplog.at_level(logging.ERROR):
        risk.transition("risk.assess")

    assert risk.status_key == "assessed"
    assert recorder.one(TransitionEvent).outcome == Outcome.OK
    assert "failed on enter" in caplog.text


def test_an_instrument_that_breaks_on_exit_does_not_break_the_move(risk_version, risk, caplog):
    with instrumented(BreaksOnExit()) as recorder, caplog.at_level(logging.ERROR):
        risk.transition("risk.assess")

    assert risk.status_key == "assessed"
    assert recorder.one(TransitionEvent).outcome == Outcome.OK
    assert "failed on exit" in caplog.text


def test_an_instrument_cannot_swallow_the_callers_error(risk_version, risk):
    """``__exit__`` returning True must not turn a refusal into a success."""
    with instrumented(Swallows()), pytest.raises(TransitionNotAllowed):
        risk.transition("risk.mitigate")

    risk.refresh_from_db()
    assert risk.status_key == "draft"


def test_strict_mode_lets_an_instruments_own_error_out(risk_version, risk):
    with (
        instrumented(BreaksOnEnter(), INSTRUMENT_STRICT=True),
        pytest.raises(RuntimeError, match="collector unreachable"),
    ):
        risk.transition("risk.assess")


def test_strict_mode_changes_nothing_for_an_instrument_that_works(risk_version, risk):
    with instrumented(INSTRUMENT_STRICT=True) as recorder:
        risk.transition("risk.assess")

    assert risk.status_key == "assessed"
    assert recorder.one(TransitionEvent).outcome == Outcome.OK


# -------------------------------------------------------------------- what leaks


def test_metadata_is_not_copied_by_default(risk_version, risk):
    with instrumented() as recorder:
        risk.transition("risk.assess", metadata={"patient": "secret"})

    assert not any(key.startswith("metadata.") for key in recorder.one(TransitionEvent).data)


def test_only_allowlisted_metadata_keys_are_copied(risk_version, risk):
    with instrumented(INSTRUMENT_METADATA_KEYS=("source",)) as recorder:
        risk.transition("risk.assess", metadata={"source": "api", "patient": "secret"})

    data = recorder.one(TransitionEvent).data
    assert data["metadata.source"] == "api"
    assert "metadata.patient" not in data


def test_the_comment_never_reaches_a_span(risk_version, risk):
    with instrumented() as recorder:
        risk.transition("risk.assess", comment="patient declined treatment")

    assert "declined" not in str(recorder.one(TransitionEvent).data)


def test_an_error_message_is_withheld_by_default(risk_version, risk):
    hook(risk_version, handler="testapp.boom", timing="after", transition="assess")

    with instrumented() as recorder, pytest.raises(RuntimeError):
        risk.transition("risk.assess")

    span = recorder.one(TransitionEvent)
    assert span.error_class == "builtins.RuntimeError"
    assert span.error_detail == ""


def test_an_error_message_is_kept_when_the_project_asks(risk_version, risk):
    hook(risk_version, handler="testapp.boom", timing="after", transition="assess")

    with instrumented(INSTRUMENT_ERROR_DETAIL=True) as recorder, pytest.raises(RuntimeError):
        risk.transition("risk.assess")

    assert recorder.one(TransitionEvent).error_detail == "boom"


def test_a_kept_error_message_is_capped(risk_version, risk):
    from vinta_state_machines.side_effects import register_side_effect

    @register_side_effect("tests.long_boom", replace=True)
    def long_boom(context: Any) -> None:
        raise RuntimeError("x" * 5000)

    hook(risk_version, handler="tests.long_boom", timing="after", transition="assess")

    with (
        instrumented(INSTRUMENT_ERROR_DETAIL=True, MAX_INSTRUMENT_ERROR_DETAIL=20) as recorder,
        pytest.raises(RuntimeError),
    ):
        risk.transition("risk.assess")

    assert len(recorder.one(TransitionEvent).error_detail) == 20


# ------------------------------------------------------------------- registration


class Counted(Instrument):
    made = 0

    def __init__(self) -> None:
        type(self).made += 1


def test_a_class_is_instantiated_once(risk_version, risk):
    Counted.made = 0
    with override_settings(STATE_MACHINES={"CACHE_GRAPHS": False, "INSTRUMENTS": [Counted]}):
        risk.transition("risk.assess")
        risk.transition("risk.mitigate")

    assert Counted.made == 1


def test_a_factory_is_called_for_its_result(risk_version, risk):
    made = Recorder()

    with override_settings(STATE_MACHINES={"CACHE_GRAPHS": False, "INSTRUMENTS": [lambda: made]}):
        risk.transition("risk.assess")

    assert made.of(TransitionEvent)


def test_a_dotted_path_is_imported(risk_version, risk, caplog):
    with (
        override_settings(
            STATE_MACHINES={
                "CACHE_GRAPHS": False,
                "INSTRUMENTS": ["vinta_state_machines.instruments.LoggingInstrument"],
            }
        ),
        caplog.at_level(logging.DEBUG, logger="vinta_state_machines.transition"),
    ):
        risk.transition("risk.assess")

    assert "transition risk.assess ok" in caplog.text


# ------------------------------------------------------------- built-in instruments


def test_the_logging_instrument_raises_its_level_for_a_refusal(risk_version, risk, caplog):
    with (
        instrumented(LoggingInstrument()),
        caplog.at_level(logging.INFO, logger="vinta_state_machines.transition"),
        pytest.raises(TransitionNotAllowed),
    ):
        risk.transition("risk.mitigate")

    assert [entry.levelno for entry in caplog.records] == [logging.INFO]
    assert caplog.records[0].state_machine["outcome"] == Outcome.DENIED
    assert caplog.records[0].state_machine["machine_key"] == "risk.status"


def test_the_logging_instrument_warns_when_a_handler_breaks(risk_version, risk, caplog):
    hook(risk_version, handler="testapp.boom", timing="after", transition="assess")

    with (
        instrumented(LoggingInstrument()),
        caplog.at_level(logging.DEBUG, logger="vinta_state_machines.side_effect"),
        pytest.raises(RuntimeError),
    ):
        risk.transition("risk.assess")

    assert caplog.records[0].levelno == logging.WARNING


class FakeStatsd:
    def __init__(self) -> None:
        self.counts: list[tuple[str, dict[str, str]]] = []
        self.timings: list[tuple[str, int, dict[str, str]]] = []

    def incr(self, name: str, tags: dict[str, str]) -> None:
        self.counts.append((name, tags))

    def timing(self, name: str, value: int, tags: dict[str, str]) -> None:
        self.timings.append((name, value, tags))


def test_the_metrics_instrument_keeps_cardinality_low(risk_version, risk):
    client = FakeStatsd()

    with instrumented(MetricsInstrument(client)):
        risk.transition("risk.assess")

    name, tags = next(item for item in client.counts if item[0] == "state_machine.transition")
    assert name == "state_machine.transition"
    assert tags == {"machine_key": "risk.status", "action": "risk.assess", "outcome": "ok"}
    assert any(item[0] == "state_machine.transition.duration" for item in client.timings)


def test_the_metrics_instrument_can_be_told_to_carry_more(risk_version, risk):
    client = FakeStatsd()

    with instrumented(
        MetricsInstrument(client, tags=("action", "to_status", "outcome"), prefix="risk_engine")
    ):
        risk.transition("risk.assess")

    _, tags = next(item for item in client.counts if item[0] == "risk_engine.transition")
    assert tags["to_status"] == "assessed"


def test_the_signal_instrument_re_emits_observations(risk_version, risk):
    seen: list[Span] = []

    @receiver(observation_finished, sender=TransitionEvent, weak=False)
    def watch(sender: Any, span: Span, **kwargs: Any) -> None:
        seen.append(span)

    try:
        with instrumented(SignalInstrument()):
            risk.transition("risk.assess")
    finally:
        observation_finished.disconnect(watch, sender=TransitionEvent)

    assert len(seen) == 1
    assert seen[0].data["action"] == "risk.assess"
    assert seen[0].outcome == Outcome.OK


def test_one_broken_receiver_does_not_stop_the_others(risk_version, risk, caplog):
    seen: list[Span] = []

    @receiver(observation_finished, sender=TransitionEvent, weak=False)
    def breaks(sender: Any, span: Span, **kwargs: Any) -> None:
        raise RuntimeError("receiver is broken")

    @receiver(observation_finished, sender=TransitionEvent, weak=False)
    def watches(sender: Any, span: Span, **kwargs: Any) -> None:
        seen.append(span)

    try:
        with instrumented(SignalInstrument()), caplog.at_level(logging.ERROR):
            risk.transition("risk.assess")
    finally:
        observation_finished.disconnect(breaks, sender=TransitionEvent)
        observation_finished.disconnect(watches, sender=TransitionEvent)

    assert risk.status_key == "assessed"
    assert len(seen) == 1
    assert "receiver is broken" in caplog.text


# ---------------------------------------------------------------------- the rest


def test_a_graph_cache_miss_is_observed(risk_version, risk):
    with instrumented() as recorder:
        risk.transition("risk.assess")

    span = recorder.of(GraphLoadEvent)[0]
    assert span.data["machine_key"] == "risk.status"
    assert span.data["states"] == 4
    assert span.data["transitions"] == 5


def test_publishing_is_observed(risk_draft, user):
    from vinta_state_machines.services import publish_version

    with instrumented() as recorder:
        publish_version(risk_draft, author=user)

    published = next(
        span for span in recorder.of(AuthoringEvent) if span.data["operation"] == "publish"
    )
    assert published.data["machine_key"] == "risk.status"
    assert published.data["actor_key"] == str(user.pk)
    assert published.data["errors"] == 0
    assert published.outcome == Outcome.OK
    # ``publish`` makes it the default, which is its own operation nested inside.
    assert {span.data["operation"] for span in recorder.of(AuthoringEvent)} == {
        "publish",
        "set_default",
    }


def test_defining_a_machine_is_observed(db, risk_definition):
    from vinta_state_machines.services import define_machine

    with instrumented() as recorder:
        define_machine(risk_definition)

    span = recorder.one(AuthoringEvent)
    assert span.data["operation"] == "define"
    assert span.data["machine_key"] == "risk.status"
    assert span.data["states"] == 4


def test_opening_and_sealing_a_batch_is_observed(waiting_run):
    from vinta_state_machines.batches import open_batch, seal

    with instrumented() as recorder:
        batch = open_batch(waiting_run, join_action="import_run.finish")
        seal(batch, total=2)

    operations = [span.data["operation"] for span in recorder.of(BatchEvent)]
    assert operations[0] == "open"
    assert "seal" in operations
    opened = recorder.of(BatchEvent)[0]
    assert opened.data["batch_pk"] == batch.pk
    assert opened.data["reused"] is False
    assert opened.data["machine_key"] == "import_run.status"


def test_a_sweep_reports_what_it_did(db):
    from vinta_state_machines.sweeper import sweep

    with instrumented() as recorder:
        sweep()

    span = recorder.one(BatchEvent)
    assert span.data["operation"] == "sweep"
    assert span.data["swept"] == 0
    assert span.data["repaired"] == 0
    # ``total`` keeps meaning "children a batch expects", so a sweep leaves it alone.
    assert span.data["total"] is None


# ------------------------------------------------------------------- inspection


def test_read_only_questions_are_not_observed_by_default(risk_version, risk):
    with instrumented() as recorder:
        available_transitions(risk)
        can_transition(risk, "risk.assess")

    assert recorder.of(InspectionEvent) == []


def test_read_only_questions_can_be_turned_on(risk_version, risk):
    with instrumented(INSTRUMENT_INSPECTION=True) as recorder:
        available_transitions(risk)
        can_transition(risk, "risk.assess")

    listed, asked = recorder.of(InspectionEvent)
    assert listed.data["operation"] == "available_transitions"
    assert listed.data["returned"] == 1
    assert asked.data["operation"] == "can_transition"
    assert asked.data["action"] == "risk.assess"
    assert asked.data["answer"] is True


# ----------------------------------------------------------------- opentelemetry


@pytest.fixture
def otel():
    """A real tracer writing into memory, so the shipped mapping is actually exercised."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("tests"), exporter


def test_otel_spans_carry_the_move_and_nest(risk_version, risk, otel):
    from opentelemetry.trace import StatusCode

    from vinta_state_machines.instruments import OpenTelemetryInstrument

    tracer, exporter = otel
    hook(risk_version, timing="after", transition="assess")

    with instrumented(OpenTelemetryInstrument(tracer)):
        risk.transition("risk.assess")

    spans = {span.name: span for span in exporter.get_finished_spans()}
    move = spans["transition risk.assess"]
    handler = spans["side_effect testapp.record"]

    assert move.attributes["state_machine.action"] == "risk.assess"
    assert move.attributes["state_machine.to_status"] == "assessed"
    assert move.attributes["state_machine.outcome"] == "ok"
    assert move.status.status_code is StatusCode.OK
    # OTel's own context propagation did the parenting; nothing was threaded through.
    assert handler.parent.span_id == move.context.span_id


def test_otel_marks_a_refusal_as_an_error_without_quoting_it(risk_version, risk, otel):
    from opentelemetry.trace import StatusCode

    from vinta_state_machines.instruments import OpenTelemetryInstrument

    tracer, exporter = otel

    with instrumented(OpenTelemetryInstrument(tracer)), pytest.raises(TransitionNotAllowed):
        risk.transition("risk.mitigate")

    span = exporter.get_finished_spans()[-1]
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description.endswith("TransitionNotAllowed")
    assert span.attributes["state_machine.outcome"] == "denied"
    # The default: no exception event, so the message never leaves the process.
    assert span.events == ()


def test_otel_records_the_exception_only_when_asked(risk_version, risk, otel):
    from vinta_state_machines.instruments import OpenTelemetryInstrument

    tracer, exporter = otel

    with (
        instrumented(OpenTelemetryInstrument(tracer, record_exception=True)),
        pytest.raises(TransitionNotAllowed),
    ):
        risk.transition("risk.mitigate")

    assert [event.name for event in exporter.get_finished_spans()[-1].events] == ["exception"]


def test_otel_drops_attributes_it_cannot_carry(otel):
    """OTel rejects a ``None`` attribute outright, and a creation edge has no from_status."""
    from vinta_state_machines.instruments import OpenTelemetryInstrument

    tracer, _ = otel
    instrument = OpenTelemetryInstrument(tracer)
    span = Span(event=_an_event(from_status=None), id="abc")
    span.set(record_pk=None)
    span.error_class = "builtins.RuntimeError"
    span.error_detail = "boom"

    attributes = instrument.attributes_for(span)

    assert "state_machine.from_status" not in attributes
    assert "state_machine.record_pk" not in attributes
    assert attributes["state_machine.action"] == "risk.assess"
    assert attributes["state_machine.error_class"] == "builtins.RuntimeError"
    assert attributes["state_machine.error_detail"] == "boom"
    assert all(value is not None for value in attributes.values())


def test_otel_honours_a_custom_prefix(otel):
    from vinta_state_machines.instruments import OpenTelemetryInstrument

    tracer, _ = otel
    instrument = OpenTelemetryInstrument(tracer, prefix="risk_engine")

    attributes = instrument.attributes_for(Span(event=_an_event(), id="abc"))

    assert attributes["risk_engine.action"] == "risk.assess"


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (BatchEvent(operation="join", batch_pk=1), "batch join"),
        (AuthoringEvent(operation="publish"), "authoring publish"),
        (
            GraphLoadEvent(machine_key="risk.status", version_pk=1, version_label="2"),
            "graph_load risk.status@2",
        ),
        (
            InspectionEvent(
                operation="can_transition",
                machine_key="risk.status",
                version_pk=1,
                version_label="1",
                scope_key="",
                target_label="testapp.risk",
                target_id="1",
                status_key="draft",
            ),
            "inspect can_transition",
        ),
        (Event(), "event"),
    ],
)
def test_every_event_names_its_own_span(event, expected):
    """The name is what an APM's trace list shows, so it says what happened, not which
    class it came from."""
    assert event.name == expected
