"""Keep the in-memory graph cache honest.

Published versions are immutable, so in practice this only fires while a draft is being
authored — but it is what makes ``clone -> edit -> publish`` visible immediately in the
same process.
"""

from __future__ import annotations

from typing import Any

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from vinta_state_machines.graph import clear_graph_cache, invalidate_graph
from vinta_state_machines.models import (
    StateMachineHook,
    StateMachineState,
    StateMachineTransition,
    StateMachineVersion,
)


@receiver(post_save, sender=StateMachineVersion)
@receiver(post_delete, sender=StateMachineVersion)
def _invalidate_version(instance: StateMachineVersion, **kwargs: Any) -> None:
    invalidate_graph(instance.pk)


@receiver(post_save, sender=StateMachineState)
@receiver(post_save, sender=StateMachineTransition)
@receiver(post_save, sender=StateMachineHook)
@receiver(post_delete, sender=StateMachineState)
@receiver(post_delete, sender=StateMachineTransition)
@receiver(post_delete, sender=StateMachineHook)
def _invalidate_parent(instance: Any, **kwargs: Any) -> None:
    invalidate_graph(instance.state_machine_version_id)


__all__ = ["clear_graph_cache"]
