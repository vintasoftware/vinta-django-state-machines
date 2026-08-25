"""Resolver for the swapped-scope run: the record's owner is its organization."""

from __future__ import annotations

from typing import Any

from tests.swapped.models import Organization


def scope_for_owner(instance: Any, config: Any) -> Any:
    owner_id = getattr(instance, "owner_id", None)
    if owner_id is None:
        return None
    return Organization.objects.filter(slug=f"o{owner_id}").first()
