"""Actors: turning whoever triggered a move into the row that records it.

Two different things are called "the actor" in this app, and keeping them apart is what
this module is for.

The **live principal** is what the caller passes to
:func:`~vinta_state_machines.engine.transition`.  Permission checks and guards need it
alive -- ``user.has_perm(...)`` is a question only a real object can answer -- so the
engine carries it around untouched for as long as it is making decisions.

The **snapshot** is what gets written down.  Groups, permissions and display names all
change, and a history that re-reads them when it is browsed reports what the actor can
do *now*, which is not what it was asked.  So the snapshot is taken synchronously, at
the moment of the move, and stored as values.

:func:`snapshot_for` is the boundary between the two.  Everything above it deals in
principals, everything below it in values.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, cast

from django.apps import apps
from django.db import models

from vinta_state_machines.conf import get_setting, identity_model_path
from vinta_state_machines.enums import IdentityType
from vinta_state_machines.types import IdentitySnapshot

logger = logging.getLogger(__name__)

__all__ = [
    "get_identity_model",
    "identity_from_user",
    "label_for_user",
    "persist_identity",
    "resolve_identity",
    "snapshot_for",
    "system_identity",
]


def get_identity_model() -> Any:
    """The concrete model ``STATE_MACHINES_IDENTITY_MODEL`` points at."""
    return apps.get_model(identity_model_path(), require_ready=False)


# ------------------------------------------------------------------- snapshotting


def system_identity(label: str = "system") -> IdentitySnapshot:
    """The actor for a move nothing and nobody was behind.

    A management command, a data migration, a scheduled sweep.  Carries no id and no
    user, but is still a real identity rather than a NULL, so every history row has an
    actor and the browse index never has to reason about missing ones.
    """
    return IdentitySnapshot(
        identity_type=IdentityType.SYSTEM,
        identity_key="",
        identity_label=label,
    )


def label_for_user(user: Any) -> str:
    """Human-readable name for a user, captured at the moment they acted.

    Reads ``get_username()`` and nothing else.  Deliberately *not* ``str(user)`` or
    ``get_full_name()``: both routinely reach through a relation -- a profile row, an
    employer, a preferred-name table -- and any query they run would be running inside
    the transaction wrapping the business move.

    Even that is wrapped, because a project is free to override the method with
    something that is not cheap.  A missing label costs a slightly less readable history
    row; an exception here would cost the transition.
    """
    try:
        getter = getattr(user, "get_username", None)
        value = getter() if callable(getter) else None
        if value:
            return str(value)
    except Exception:
        logger.warning(
            "Could not read a display name for the transition actor; recording the "
            "move without one.",
            exc_info=True,
        )
    return ""


def identity_from_user(user: Any) -> IdentitySnapshot:
    """Snapshot a Django user as the actor of a move.

    Captures groups and permissions **now**, in the request, because both are mutable
    state that must not be re-read later.

    The authorization half costs up to two queries.  In the common case one of them is
    already paid for -- a transition with a ``required_permission`` has just called
    ``has_perm``, which populates the backend's permission cache on the user object --
    but the group read is genuinely extra.  Projects that do not want it set
    ``CAPTURE_AUTHORIZATION_SNAPSHOT`` to ``False`` and keep the identifying columns.
    """
    if user is None:
        return system_identity()
    snapshot = IdentitySnapshot(
        identity_type=IdentityType.USER,
        identity_key=str(user.pk) if user.pk is not None else "",
        identity_label=label_for_user(user),
        user_id=user.pk,
        is_staff=bool(getattr(user, "is_staff", False)),
        is_superuser=bool(getattr(user, "is_superuser", False)),
    )
    if not get_setting("CAPTURE_AUTHORIZATION_SNAPSHOT"):
        return snapshot
    return replace(
        snapshot,
        group_names=_group_names(user),
        permission_keys=_permission_keys(user),
    )


def _group_names(user: Any) -> list[str]:
    groups = getattr(user, "groups", None)
    if groups is None:
        return []
    try:
        return sorted(groups.values_list("name", flat=True))
    except Exception:
        logger.warning("Could not read the actor's groups; recording without them.", exc_info=True)
        return []


def _permission_keys(user: Any) -> list[str]:
    getter = getattr(user, "get_all_permissions", None)
    if not callable(getter):
        return []
    try:
        return sorted(getter())
    except Exception:
        logger.warning(
            "Could not read the actor's permissions; recording without them.", exc_info=True
        )
        return []


def snapshot_for(actor: Any) -> IdentitySnapshot:
    """Turn whatever the caller passed as the actor into a portable snapshot.

    Understands the four things a caller reasonably has to hand:

    * ``None`` -- the system acting on its own behalf.
    * an :class:`~vinta_state_machines.types.IdentitySnapshot` -- already a snapshot,
      returned unchanged, which is how a caller records an actor this app cannot
      introspect (an API token, a webhook sender).
    * a saved identity row -- flattened back to its values.
    * anything else -- treated as a Django user.

    ``IDENTITY_RESOLVER`` replaces this wholesale for a project whose principals need
    different treatment.
    """
    resolver = get_setting("IDENTITY_RESOLVER")
    if resolver is not None:
        return cast("IdentitySnapshot", resolver(actor))
    return default_snapshot_for(actor)


def default_snapshot_for(actor: Any) -> IdentitySnapshot:
    """The dispatch :func:`snapshot_for` uses when no ``IDENTITY_RESOLVER`` is set.

    Exposed separately so a custom resolver can handle the cases it cares about and
    delegate the rest here rather than reimplementing them.
    """
    if actor is None:
        return system_identity()
    if isinstance(actor, IdentitySnapshot):
        return actor
    if isinstance(actor, get_identity_model()):
        return cast("IdentitySnapshot", actor.to_snapshot())
    if isinstance(actor, models.Model) or hasattr(actor, "get_username"):
        # An unsaved or anonymous user identifies nobody, so it records as the system
        # rather than as a user with an empty key.
        if not _is_saved_user(actor):
            return system_identity()
        return identity_from_user(actor)
    raise TypeError(
        f"Cannot record {actor!r} as an actor. Pass a user, an identity row, an "
        "IdentitySnapshot, or None; or set STATE_MACHINES['IDENTITY_RESOLVER']."
    )


def _is_saved_user(user: Any) -> bool:
    return (
        isinstance(user, models.Model)
        and user.pk is not None
        and getattr(user, "is_authenticated", True)
    )


# ---------------------------------------------------------------------- persisting


def persist_identity(snapshot: IdentitySnapshot) -> Any:
    """Write a snapshot as a new identity row.

    Always an insert, never a lookup.  These rows are one-per-reference by design: two
    moves by the same person a month apart are two rows, because the groups and
    permissions between them may differ and the whole point of the snapshot is to say
    which ones applied at the time.

    The row is built by the model's own
    :meth:`~vinta_state_machines.models.AbstractStateMachineIdentity.from_snapshot`, so
    a swapped in model with extra columns fills them without this function knowing.
    """
    row = get_identity_model().from_snapshot(snapshot)
    row.save()
    return row


def resolve_identity(actor: Any) -> Any:
    """Snapshot a principal and write it down, in one step.

    What the engine calls at the moment it records a move.  A row already saved by the
    caller is reused as-is rather than copied, so passing the identity returned by an
    earlier call records both moves against the same snapshot -- the way to say "these
    happened together" when a project fans one action out into several transitions.
    """
    if isinstance(actor, get_identity_model()) and actor.pk is not None:
        return actor
    return persist_identity(snapshot_for(actor))
