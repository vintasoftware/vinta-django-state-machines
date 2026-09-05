"""Which registered keys a tenant is allowed to wire up.

A :class:`~vinta_state_machines.models.ScopeCapabilityRule` row says that one scope may,
or may not, use side effects, actions or guards whose key matches a pattern.  A scope
with no rows is unrestricted, so an installation that never writes one behaves exactly
as it did before this module existed.

Three things are worth knowing before reading the code.

**Deny wins, always.**  Not "the most specific rule wins": a deny is a statement that
something must not happen, and a policy where a broader allow can quietly overturn one
is a policy nobody can audit by reading it.

**Global and tenant rules intersect.**  This is deliberately *not* the fallback
:func:`~vinta_state_machines.scopes.resolve_machine` performs.  A global machine is a
default a tenant may replace; a global rule is the installation speaking, and it is not
a tenant's to override.  A key has to clear both policies.

**This is checked while authoring, not while running.**  The engine never consults these
rules.  A published version is immutable and records pin it, so a policy row that could
change what a pinned version does at run time would take that guarantee away and leave
"what ran when this record moved" underivable from the version.  The rules are enforced
where the wiring is *written*: the editor's catalogs, the editor's apply path, and
:func:`~vinta_state_machines.services.define_machine`.  Publication only warns, because
a policy may have tightened since a draft was authored and blocking an already-approved
graph on a rule written afterwards is a support ticket rather than a control.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from vinta_state_machines.conf import get_setting
from vinta_state_machines.enums import CapabilityResource, RuleEffect, ScopeType
from vinta_state_machines.exceptions import CapabilityDenied

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "CapabilityPolicy",
    "assert_permitted",
    "denial_reason",
    "is_permitted",
    "permitted_keys",
    "policies_for",
    "policy_for",
]


@dataclass(frozen=True)
class CapabilityPolicy:
    """One scope's rules for one resource, resolved and ready to answer questions.

    Built by :func:`policy_for`, which is where the global scope's rules are folded in.
    Holding the two layers apart rather than merging their patterns is what lets
    :meth:`reason` say *which* policy refused, which is most of what makes a refusal
    actionable.
    """

    resource: str
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    global_allow: tuple[str, ...] = ()
    global_deny: tuple[str, ...] = ()

    @property
    def unrestricted(self) -> bool:
        """Whether this policy has nothing to say, which is the common case."""
        return not (self.allow or self.deny or self.global_allow or self.global_deny)

    def permits(self, key: str) -> bool:
        """Whether ``key`` clears both layers."""
        return self.reason(key) is None

    def reason(self, key: str) -> str | None:
        """Why ``key`` is refused, or ``None`` if it is not.

        The order matters and is the precedence rule in one place: a deny at either
        layer settles it, and only then does an allow list, if there is one, have to
        be matched.
        """
        if _matches(key, self.global_deny):
            return f"the installation denies {self.resource} {key!r}"
        if _matches(key, self.deny):
            return f"this scope's policy denies {self.resource} {key!r}"
        if self.global_allow and not _matches(key, self.global_allow):
            return f"{key!r} is not on the installation's {self.resource} allow list"
        if self.allow and not _matches(key, self.allow):
            return f"{key!r} is not on this scope's {self.resource} allow list"
        return None

    def filter(self, keys: Iterable[str]) -> list[str]:
        """Those of ``keys`` this policy permits, in the order given."""
        if self.unrestricted:
            return list(keys)
        return [key for key in keys if self.permits(key)]


def _matches(key: str, patterns: tuple[str, ...]) -> bool:
    # ``fnmatchcase`` rather than ``fnmatch``: keys are lowercase by convention but the
    # convention is not enforced, and a policy that quietly case-folds is a policy that
    # can be slipped past on a case-sensitive database.
    return any(fnmatchcase(key, pattern) for pattern in patterns)


ALL_RESOURCES: tuple[str, ...] = tuple(CapabilityResource.values)


def policies_for(
    scope: Any, resources: Iterable[str] = ALL_RESOURCES
) -> dict[str, CapabilityPolicy]:
    """Resolve ``scope``'s rules for several resources in **one** query.

    Reach for this rather than calling :func:`policy_for` in a loop: the callers that
    matter -- the editor's document pass, ``validate_version``, ``define_machine`` --
    all check several keys against several resources at once, and per-key resolution
    turns one small query into one per transition.

    ``scope`` may be a scope instance, a primary key, or ``None``, which means the
    record has no tenant and only the installation's own rules apply.
    """
    from vinta_state_machines.models import ScopeCapabilityRule

    wanted = tuple(resources)
    if not get_setting("ENFORCE_CAPABILITY_POLICY"):
        return {resource: CapabilityPolicy(resource=resource) for resource in wanted}

    scope_pk = getattr(scope, "pk", scope)
    buckets: dict[str, dict[str, list[str]]] = {
        resource: {"allow": [], "deny": [], "global_allow": [], "global_deny": []}
        for resource in wanted
    }
    rows = ScopeCapabilityRule.objects.filter(resource__in=wanted).values_list(
        "resource", "scope__scope_type", "scope_id", "effect", "pattern"
    )
    for resource, scope_type, row_scope_pk, effect, pattern in rows:
        denied = effect == RuleEffect.DENY
        if scope_type == ScopeType.GLOBAL:
            name = "global_deny" if denied else "global_allow"
        elif scope_pk is not None and row_scope_pk == scope_pk:
            name = "deny" if denied else "allow"
        else:
            continue
        buckets[resource][name].append(pattern)

    return {
        resource: CapabilityPolicy(
            resource=resource,
            allow=tuple(bucket["allow"]),
            deny=tuple(bucket["deny"]),
            global_allow=tuple(bucket["global_allow"]),
            global_deny=tuple(bucket["global_deny"]),
        )
        for resource, bucket in buckets.items()
    }


def policy_for(scope: Any, resource: str) -> CapabilityPolicy:
    """Resolve the rules that govern ``scope`` for one resource.

    One query.  Checking more than one resource, or more than one key, wants
    :func:`policies_for` instead.
    """
    return policies_for(scope, (resource,))[resource]


def has_bypass(actor: Any) -> bool:
    """Whether ``actor`` may wire up keys their scope's policy forbids.

    Routed through ``PERMISSION_CHECKER`` when a project has set one, so an
    installation with its own authorization backend gets asked rather than bypassed.
    """
    if actor is None:
        return False
    permission = get_setting("CAPABILITY_BYPASS_PERMISSION")
    checker = get_setting("PERMISSION_CHECKER")
    if checker is not None:
        return bool(checker(actor, permission, None))
    has_perm = getattr(actor, "has_perm", None)
    return bool(has_perm(permission)) if callable(has_perm) else False


def is_permitted(scope: Any, resource: str, key: str, *, actor: Any = None) -> bool:
    """Whether ``scope`` may wire up ``key``, or ``actor`` may do it on their behalf."""
    return denial_reason(scope, resource, key, actor=actor) is None


def denial_reason(scope: Any, resource: str, key: str, *, actor: Any = None) -> str | None:
    """Why ``key`` is refused for ``scope``, or ``None`` if it is not."""
    if has_bypass(actor):
        return None
    return policy_for(scope, resource).reason(key)


def assert_permitted(scope: Any, resource: str, key: str, *, actor: Any = None) -> None:
    """Raise :class:`CapabilityDenied` unless ``key`` is permitted for ``scope``."""
    reason = denial_reason(scope, resource, key, actor=actor)
    if reason is not None:
        raise CapabilityDenied(reason.capitalize() + ".", resource=resource, key=key)


def permitted_keys(
    scope: Any, resource: str, keys: Iterable[str], *, actor: Any = None
) -> list[str]:
    """Those of ``keys`` that ``scope`` may wire up, in the order given.

    What the editor's catalog endpoints hand the canvas: a tenant is offered the keys
    it can actually use rather than a list two thirds of which will be refused on save.
    """
    if has_bypass(actor):
        return list(keys)
    return policy_for(scope, resource).filter(keys)
