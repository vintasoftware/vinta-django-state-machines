"""Portable references: what a scope and an actor are, independent of any row.

Nothing here imports a model, and that is the point.  These are the values that cross a
database boundary in an exported machine definition, get compared in tests, and are
captured *synchronously* in a request before anything is written -- so they hold values
rather than rows.

Two pairs make up the vocabulary:

* :class:`ScopeRef` -- which tenant something belongs to, and :class:`ScopeKey`, its
  identifying half.
* :class:`IdentitySnapshot` -- who acted and what they could do at the time, and
  :class:`IdentityRef`, its identifying half.

The split matters: snapshot fields never identify anything.  A display name or a group
list is what an actor *happened to carry* at one moment, so it is recorded but never
matched on, while ``(identity_type, identity_key)`` is stable enough to look a principal
up by across databases.

These dataclasses mirror ``vinta_audit_logs.types`` field for field.  The duplication is
deliberate -- a state machine install should not have to carry an audit log app -- but
the shapes are meant to stay interchangeable, so a change here wants the same change
there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vinta_state_machines.enums import IdentityType, ScopeType

__all__ = ["IdentityRef", "IdentitySnapshot", "ScopeKey", "ScopeRef"]


@dataclass(frozen=True)
class ScopeRef:
    """A tenant, as portable values rather than a primary key.

    ``scope_key`` is what an exported machine carries and what an import resolves back
    to a row, so it has to be stable for the life of the scope: definitions already
    exported under an old key do not follow a new one.

    ``label`` rides along for the benefit of a scope row created on first sight.  It is
    not part of the scope's identity and never participates in a lookup.
    """

    scope_type: str = ScopeType.GLOBAL
    scope_key: str = ""
    label: str = ""

    @property
    def key(self) -> ScopeKey:
        """This scope's identity, label dropped -- ready to look up with."""
        return ScopeKey(scope_type=self.scope_type, scope_key=self.scope_key)

    @classmethod
    def global_scope(cls) -> ScopeRef:
        """The scope every tenant falls back to when it has no machine of its own."""
        return cls(scope_type=ScopeType.GLOBAL, scope_key="")


@dataclass(frozen=True)
class ScopeKey:
    """The identifying half of a scope: the pair a scope row is unique on."""

    scope_type: str
    scope_key: str


@dataclass(frozen=True)
class IdentitySnapshot:
    """Who acted, captured at the moment they acted.

    Captured in the request rather than read back later, because every field here is
    mutable state that may have changed -- or been deleted -- by the time anyone reads
    the history.  A trail that re-reads an actor's groups when it is browsed reports the
    groups they have now, which is not the question it was asked.

    ``identity_type`` and ``identity_key`` together identify the principal; ids are only
    unique within a type, so user 7 and API token 7 are different actors and the pair
    always travels together.  ``identity_key`` is ``""`` for a principal with no id at
    all -- the system acting on its own behalf.

    ``metadata`` is the project's extension point: a membership role, a token's scopes,
    whatever else the history should remember about this actor.  Stored verbatim.
    """

    identity_type: str = IdentityType.SYSTEM
    identity_key: str = ""
    identity_label: str = ""
    user_id: int | str | None = None
    is_staff: bool = False
    is_superuser: bool = False
    group_names: list[str] = field(default_factory=list)
    permission_keys: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> IdentityRef:
        """This actor's identity, snapshot fields dropped -- ready to look up with."""
        return IdentityRef(identity_type=self.identity_type, identity_key=self.identity_key)


@dataclass(frozen=True)
class IdentityRef:
    """The identifying half of an actor.

    Both fields are required.  ``identity_key=""`` means the principal genuinely has no
    id, not "any id of this type".
    """

    identity_type: str
    identity_key: str
