"""Guards: the boolean condition a transition carries.

A guard is either

* a **named guard** — ``"@amount_within_limit"`` — resolved from the registry populated
  by :func:`register_guard`, or
* a **safe expression** — ``"obj.amount <= 1000 and obj.owner_id"`` — evaluated against a
  restricted subset of Python with no imports, no attribute access to private names, no
  function calls beyond a small whitelist, and no statements.

The expression form is convenient for the catalog UI; the named form is what you reach
for as soon as a condition needs real code.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable, Mapping
from typing import Any

from vinta_state_machines.conf import get_setting
from vinta_state_machines.registry import Registry

Guard = Callable[..., bool]

guard_registry: Registry[Guard] = Registry(kind="guard")
"""The process-wide registry of named guards."""

NAMED_GUARD_PREFIX = "@"


def register_guard(key: str, *, replace: bool = False) -> Callable[[Guard], Guard]:
    """Register a named guard, referenced from the catalog as ``"@<key>"``.

    The function is called with the evaluation context as keyword arguments
    (``obj``, ``user``, ``action``, ``from_status``, ``to_status``, ``metadata``) and
    must return something truthy to allow the transition.
    """
    return guard_registry(key, replace=replace)


class GuardSyntaxError(ValueError):
    """A guard expression is not valid, or uses a construct that is not allowed."""


# ------------------------------------------------------------------ safe evaluation

_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_CMP_OPS: dict[type[ast.cmpop], Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}

_UNARY_OPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.Not: operator.not_,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_SAFE_CALLABLES: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "float": float,
    "int": int,
    "len": len,
    "max": max,
    "min": min,
    "round": round,
    "str": str,
    "sum": sum,
    "sorted": sorted,
}

_MAX_POW = 1_000_000
_MAX_DEPTH = 25


class _SafeEvaluator(ast.NodeVisitor):
    """Walks a parsed expression, refusing anything not explicitly allowed."""

    def __init__(self, context: Mapping[str, Any]) -> None:
        self.context = context
        self.depth = 0

    def visit(self, node: ast.AST) -> Any:
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            raise GuardSyntaxError("Guard expression nests too deeply.")
        try:
            return super().visit(node)
        finally:
            self.depth -= 1

    def generic_visit(self, node: ast.AST) -> Any:
        raise GuardSyntaxError(f"{type(node).__name__} is not allowed in a guard expression.")

    # -- literals & containers
    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def visit_List(self, node: ast.List) -> Any:
        return [self.visit(item) for item in node.elts]

    def visit_Tuple(self, node: ast.Tuple) -> Any:
        return tuple(self.visit(item) for item in node.elts)

    def visit_Set(self, node: ast.Set) -> Any:
        return {self.visit(item) for item in node.elts}

    def visit_Dict(self, node: ast.Dict) -> Any:
        return {
            self.visit(key) if key is not None else None: self.visit(value)
            for key, value in zip(node.keys, node.values, strict=True)
        }

    # -- operators
    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = self.visit(value)
                if not result:
                    return result
            return result
        result = False
        for value in node.values:
            result = self.visit(value)
            if result:
                return result
        return result

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        try:
            func = _UNARY_OPS[type(node.op)]
        except KeyError:
            raise GuardSyntaxError(f"Unary {type(node.op).__name__} is not allowed.") from None
        return func(self.visit(node.operand))

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        try:
            func = _BIN_OPS[type(node.op)]
        except KeyError:
            raise GuardSyntaxError(f"Operator {type(node.op).__name__} is not allowed.") from None
        left, right = self.visit(node.left), self.visit(node.right)
        if isinstance(node.op, ast.Pow) and isinstance(right, int) and right > _MAX_POW:
            raise GuardSyntaxError("Exponent is too large for a guard expression.")
        return func(left, right)

    def visit_Compare(self, node: ast.Compare) -> Any:
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            try:
                func = _CMP_OPS[type(op)]
            except KeyError:
                raise GuardSyntaxError(f"Comparison {type(op).__name__} is not allowed.") from None
            right = self.visit(comparator)
            if not func(left, right):
                return False
            left = right
        return True

    def visit_IfExp(self, node: ast.IfExp) -> Any:
        return self.visit(node.body) if self.visit(node.test) else self.visit(node.orelse)

    # -- names, attributes, items, calls
    def visit_Name(self, node: ast.Name) -> Any:
        if node.id.startswith("_"):
            raise GuardSyntaxError("Private names are not allowed in a guard expression.")
        if node.id in self.context:
            return self.context[node.id]
        if node.id in _SAFE_CALLABLES:
            return _SAFE_CALLABLES[node.id]
        if node.id in ("True", "False", "None"):  # pragma: no cover - parsed as Constant
            return {"True": True, "False": False, "None": None}[node.id]
        raise GuardSyntaxError(f"Unknown name {node.id!r} in guard expression.")

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        if node.attr.startswith("_"):
            raise GuardSyntaxError("Private attributes are not allowed in a guard expression.")
        value = getattr(self.visit(node.value), node.attr)
        if not callable(value):
            return value
        if getattr(value, "guard_safe", False):
            return value()
        raise GuardSyntaxError(
            f"{node.attr!r} is callable. Guards never invoke arbitrary methods; expose "
            "it as a property, or decorate it with @guard_callable to opt in."
        )

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        return self.visit(node.value)[self.visit(node.slice)]

    def visit_Call(self, node: ast.Call) -> Any:
        if node.keywords:
            raise GuardSyntaxError("Keyword arguments are not allowed in a guard expression.")
        if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_CALLABLES:
            raise GuardSyntaxError(
                "Only these calls are allowed in a guard expression: "
                + ", ".join(sorted(_SAFE_CALLABLES))
                + "."
            )
        func = _SAFE_CALLABLES[node.func.id]
        return func(*(self.visit(arg) for arg in node.args))


def guard_callable(func: Any) -> Any:
    """Mark a no-argument method as safe to call from a guard expression.

    Guards refuse to invoke methods, because ``obj.delete`` reads exactly like an
    attribute and a great many useful methods are destructive.  Opting a method in makes
    ``obj.is_large`` evaluate it, the way a Django template would::

        class Risk(models.Model):
            @guard_callable
            def is_large(self):
                return self.amount > 1000
    """
    func.guard_safe = True
    return func


def evaluate_expression(expression: str, context: Mapping[str, Any]) -> bool:
    """Evaluate a guard expression against ``context`` and coerce the result to bool."""
    max_length = get_setting("MAX_GUARD_EXPRESSION_LENGTH")
    if len(expression) > max_length:
        raise GuardSyntaxError(f"Guard expression exceeds {max_length} characters.")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise GuardSyntaxError(f"Invalid guard expression: {exc.msg}") from exc
    return bool(_SafeEvaluator(context).visit(tree))


def evaluate(guard: str, context: Mapping[str, Any]) -> bool:
    """Evaluate ``guard``, dispatching to the registry or the safe evaluator.

    An empty guard always passes.
    """
    guard = guard.strip()
    if not guard:
        return True
    if guard.startswith(NAMED_GUARD_PREFIX):
        func = guard_registry.get(guard[1:].strip())
        return bool(func(**context))
    if not get_setting("ALLOW_GUARD_EXPRESSIONS"):
        raise GuardSyntaxError(
            "Guard expressions are disabled; reference a registered guard as '@name'."
        )
    return evaluate_expression(guard, context)


def validate_guard(guard: str) -> None:
    """Raise :class:`GuardSyntaxError` if ``guard`` could never be evaluated.

    Used by model validation and by ``publish_version`` so a broken guard is caught at
    authoring time rather than the first time a record tries to move.
    """
    guard = guard.strip()
    if not guard:
        return
    if guard.startswith(NAMED_GUARD_PREFIX):
        guard_registry.get(guard[1:].strip())
        return
    if not get_setting("ALLOW_GUARD_EXPRESSIONS"):
        raise GuardSyntaxError(
            "Guard expressions are disabled; reference a registered guard as '@name'."
        )
    if len(guard) > get_setting("MAX_GUARD_EXPRESSION_LENGTH"):
        raise GuardSyntaxError("Guard expression is too long.")
    try:
        ast.parse(guard, mode="eval")
    except SyntaxError as exc:
        raise GuardSyntaxError(f"Invalid guard expression: {exc.msg}") from exc
