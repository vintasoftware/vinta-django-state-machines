"""The guard evaluator: expressive enough to be useful, narrow enough to be safe."""

from __future__ import annotations

import pytest

from tests.testapp.models import Risk
from vinta_state_machines.guards import (
    GuardSyntaxError,
    evaluate,
    evaluate_expression,
    guard_registry,
    register_guard,
    validate_guard,
)


@pytest.fixture
def obj() -> Risk:
    return Risk(title="Retention", amount=500, owner_id=7)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("obj.amount <= 1000", True),
        ("obj.amount > 1000", False),
        ("obj.amount > 100 and obj.owner_id", True),
        ("obj.amount > 100 or obj.amount < 0", True),
        ("not obj.is_large", True),
        ("obj.is_large", False),
        ("obj.amount in (500, 600)", True),
        ("obj.owner_id is not None", True),
        ("100 < obj.amount < 1000", True),
        ("len(obj.title) > 3", True),
        ("obj.amount * 2 == 1000", True),
        ("'Retention' in obj.title", True),
        ("obj.has_owner", True),
        ("obj.amount if obj.owner_id else 0", True),
        ("metadata['ticket'] == 'SEC-1'", True),
        ("user is None", True),
    ],
)
def test_supported_expressions(obj, expression, expected):
    context = {"obj": obj, "user": None, "metadata": {"ticket": "SEC-1"}}
    assert evaluate_expression(expression, context) is expected


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('true')",
        "obj.__class__.__mro__",
        "obj._meta.db_table",
        "open('/etc/passwd')",
        "[x for x in (1, 2)]",
        "lambda: 1",
        "obj.delete()",
        "obj.save()",
        "(1).__class__",
        "exec('x=1')",
        "obj.title := 'x'",
    ],
)
def test_refused_expressions(obj, expression):
    with pytest.raises(GuardSyntaxError):
        evaluate_expression(expression, {"obj": obj})


def test_an_unknown_name_is_refused_rather_than_treated_as_false(obj):
    with pytest.raises(GuardSyntaxError, match="Unknown name 'whatever'"):
        evaluate_expression("whatever > 1", {"obj": obj})


def test_a_huge_exponent_is_refused(obj):
    with pytest.raises(GuardSyntaxError, match="Exponent is too large"):
        evaluate_expression("2 ** 99999999", {})


def test_an_overlong_expression_is_refused(obj):
    with pytest.raises(GuardSyntaxError, match="exceeds"):
        evaluate_expression("obj.amount > 1 and " * 200 + "True", {"obj": obj})


def test_deep_nesting_is_refused(obj):
    with pytest.raises(GuardSyntaxError, match="nests too deeply"):
        evaluate_expression("not " * 30 + "True", {})


def test_an_opted_in_method_is_called_like_a_property(obj):
    assert evaluate_expression("obj.is_large", {"obj": obj}) is False
    obj.amount = 5000
    assert evaluate_expression("obj.is_large", {"obj": obj}) is True


def test_a_method_that_did_not_opt_in_is_never_invoked(obj):
    """``obj.delete`` takes only defaults, so a naive evaluator would delete the row."""
    with pytest.raises(GuardSyntaxError, match="decorate it with @guard_callable"):
        evaluate_expression("obj.delete", {"obj": obj})


def test_an_empty_guard_always_passes():
    assert evaluate("", {}) is True
    assert evaluate("   ", {}) is True


def test_a_named_guard_receives_the_context_as_keywords(obj):
    seen = {}

    @register_guard("tests.capture")
    def capture(**context):
        seen.update(context)
        return context["obj"].amount < 1000

    try:
        assert evaluate("@tests.capture", {"obj": obj, "user": None}) is True
        assert seen["obj"] is obj
    finally:
        guard_registry.unregister("tests.capture")


def test_a_named_guard_that_is_not_registered_is_reported():
    with pytest.raises(KeyError, match="tests.missing"):
        evaluate("@tests.missing", {})


def test_expressions_can_be_switched_off_entirely(settings, obj):
    settings.STATE_MACHINES = {"ALLOW_GUARD_EXPRESSIONS": False, "CACHE_GRAPHS": False}
    with pytest.raises(GuardSyntaxError, match="disabled"):
        evaluate("obj.amount > 1", {"obj": obj})


def test_validate_guard_accepts_what_evaluate_would_run(obj):
    validate_guard("obj.amount <= 1000")
    validate_guard("")


def test_validate_guard_rejects_a_syntax_error():
    with pytest.raises(GuardSyntaxError, match="Invalid guard expression"):
        validate_guard("obj.amount ===")
