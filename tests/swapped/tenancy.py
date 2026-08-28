"""Resolver for the swapped-model run: the record's owner is its organization."""

from __future__ import annotations

from typing import Any

from tests.swapped.models import OrganizationScope
from vinta_state_machines.enums import ScopeType


def scope_for_owner(instance: Any, config: Any) -> Any:
    """The scope row wrapping the record's organization, or ``None`` for an unowned one.

    Returns the *scope*, not the organization: the scope is the library's adapter, which
    is what lets ``Organization`` stay a plain project model with no library columns on
    it.
    """
    owner_id = getattr(instance, "owner_id", None)
    if owner_id is None:
        return None
    return OrganizationScope.objects.filter(
        scope_type=ScopeType.SCOPED, organization__slug=f"o{owner_id}"
    ).first()
