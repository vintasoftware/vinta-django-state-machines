"""Scope and identity models a project might swap in, with columns of their own.

The suite runs a second time against these to prove the claim the library makes: a
project can point ``STATE_MACHINES_SCOPE_MODEL`` and ``STATE_MACHINES_IDENTITY_MODEL``
at models carrying real foreign keys and extra columns, and nothing in the engine, the
history or the export commands has to know.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import models

from vinta_state_machines.enums import ScopeType
from vinta_state_machines.models import (
    AbstractStateMachineIdentity,
    AbstractStateMachineScope,
)

if TYPE_CHECKING:
    from vinta_state_machines.types import IdentitySnapshot


class Organization(models.Model):
    """The tenant this test project scopes its machines by.

    An ordinary project model: it knows nothing about state machines, which is the
    point.  The scope model below is the adapter between it and the library.
    """

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=200, blank=True)

    class Meta:
        app_label = "swapped"

    def __str__(self) -> str:
        return self.slug


class OrganizationScope(AbstractStateMachineScope[Organization]):
    """A scope that is one organization, or the whole installation.

    ``PROTECT``, not ``CASCADE``: deleting an organization must not delete the record of
    what happened inside it.
    """

    organization = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="state_machine_scopes",
    )

    class Meta(AbstractStateMachineScope.Meta):
        abstract = False
        app_label = "swapped"
        swappable = "STATE_MACHINES_SCOPE_MODEL"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(scope_type=ScopeType.GLOBAL, organization__isnull=True)
                    | ~models.Q(scope_type=ScopeType.GLOBAL) & models.Q(organization__isnull=False)
                ),
                name="swapped_scope_type_and_organization_agree",
            ),
            models.UniqueConstraint(
                fields=("scope_type", "scope_key"),
                name="swapped_scope_unique_key_per_type",
            ),
        ]

    @property
    def scope(self) -> Organization | None:
        return self.organization

    @scope.setter
    def scope(self, value: Organization | None) -> None:
        self.organization = value
        self.scope_type = ScopeType.GLOBAL if value is None else ScopeType.SCOPED

    @scope.deleter
    def scope(self) -> None:
        self.organization = None
        self.scope_type = ScopeType.GLOBAL

    def build_scope_key(self) -> str:
        """The organization's slug, prefixed; ``""`` for the global scope.

        Deliberately not the primary key: an exported machine carries this string to
        another database, where the pk would mean something else or nothing at all.
        """
        if self.organization_id is None:
            return ""
        return f"org:{self.organization.slug}"


class OrganizationIdentity(AbstractStateMachineIdentity):
    """An identity with a column of its own, to prove the hook reaches it."""

    #: Whatever a project wants to filter actors by. Here: the department the actor
    #: belonged to at the moment they acted -- a snapshot like everything around it.
    department = models.CharField(max_length=64, blank=True)

    class Meta(AbstractStateMachineIdentity.Meta):
        abstract = False
        app_label = "swapped"
        swappable = "STATE_MACHINES_IDENTITY_MODEL"

    @classmethod
    def from_snapshot(cls, snapshot: IdentitySnapshot) -> OrganizationIdentity:
        """Promote one key out of ``metadata`` into a real column.

        The extension point a project swaps the model out for: the library hands over a
        portable snapshot and the model decides what to do with it.
        """
        row: Any = super().from_snapshot(snapshot)
        row.department = row.metadata.pop("department", "")
        return row
