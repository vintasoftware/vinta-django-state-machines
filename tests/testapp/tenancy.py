"""A resolver for the tests: the record's owner is its tenant.

Real projects usually read the tenant off the request through a middleware, but reading
it off the instance is the shape that exercises the interesting path -- the resolver is
handed the record being created, before it has been saved.
"""

from __future__ import annotations

from typing import Any

from vinta_state_machines.enums import ScopeType
from vinta_state_machines.models import StateMachineScope


def scope_for_owner(instance: Any, config: Any) -> Any:
    """Map ``instance.owner_id`` onto a scope, or ``None`` for an unowned record.

    ``None`` means "this record has no tenant", which the engine reads as a fallback to
    the global machine -- not as the global scope row itself.
    """
    owner_id = getattr(instance, "owner_id", None)
    if owner_id is None:
        return None
    return StateMachineScope.objects.filter(
        scope_type=ScopeType.SCOPED, scope_key=f"org.{owner_id}"
    ).first()


def scope_key_for_owner(instance: Any, config: Any) -> Any:
    """Same, but returning the portable key so the string branch gets covered too."""
    owner_id = getattr(instance, "owner_id", None)
    return None if owner_id is None else f"org.{owner_id}"
