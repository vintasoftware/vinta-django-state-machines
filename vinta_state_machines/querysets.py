"""Query helpers for the versioned models and the history log."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.contenttypes.models import ContentType
from django.db import models

if TYPE_CHECKING:
    # Quoted below, because models.py imports this module: the real import would cycle.
    from vinta_state_machines.models import (  # noqa: F401
        StateMachineVersion,
        StatusBatch,
        StatusTransition,
    )


class StateMachineVersionQuerySet(models.QuerySet["StateMachineVersion"]):
    def draft(self) -> StateMachineVersionQuerySet:
        return self.filter(lifecycle="draft")

    def published(self) -> StateMachineVersionQuerySet:
        return self.filter(lifecycle="published")

    def archived(self) -> StateMachineVersionQuerySet:
        return self.filter(lifecycle="archived")

    def for_machine(self, key: str) -> StateMachineVersionQuerySet:
        return self.filter(state_machine__key=key)

    def with_graph(self) -> StateMachineVersionQuerySet:
        """Prefetch everything :func:`~vinta_state_machines.graph.build_graph` reads."""
        return self.select_related("state_machine__scope").prefetch_related(
            "states__status", "transitions__action_type", "hooks"
        )


class StatusTransitionQuerySet(models.QuerySet["StatusTransition"]):
    def for_object(
        self, instance: models.Model, *, status_field: str | None = None
    ) -> StatusTransitionQuerySet:
        """History of one record, newest first."""
        queryset = self.filter(
            target_type=ContentType.objects.get_for_model(instance, for_concrete_model=False),
            target_id=str(instance.pk),
        )
        if status_field is not None:
            queryset = queryset.filter(status_field=status_field)
        return queryset

    def for_model(self, model: type[models.Model], **filters: Any) -> StatusTransitionQuerySet:
        return self.filter(
            target_type=ContentType.objects.get_for_model(model, for_concrete_model=False),
            **filters,
        )

    def entering(self, status_key: str) -> StatusTransitionQuerySet:
        return self.filter(to_status__key=status_key)

    def leaving(self, status_key: str) -> StatusTransitionQuerySet:
        return self.filter(from_status__key=status_key)

    def with_related(self) -> StatusTransitionQuerySet:
        return self.select_related(
            "from_status",
            "to_status",
            "action_type",
            "transition",
            "state_machine_version",
            "actor",
        )


class StatusBatchQuerySet(models.QuerySet["StatusBatch"]):
    def for_object(
        self, instance: models.Model, *, status_field: str | None = None
    ) -> StatusBatchQuerySet:
        """Every batch ever opened on one record, newest first."""
        queryset = self.filter(
            target_type=ContentType.objects.get_for_model(instance, for_concrete_model=False),
            target_id=str(instance.pk),
        )
        if status_field is not None:
            queryset = queryset.filter(status_field=status_field)
        return queryset

    def live(self) -> StatusBatchQuerySet:
        """Batches that still own their record: open, or already claimed for the join."""
        return self.filter(lifecycle__in=("open", "joining"))

    def open(self) -> StatusBatchQuerySet:
        return self.filter(lifecycle="open")

    def joining(self) -> StatusBatchQuerySet:
        return self.filter(lifecycle="joining")

    def complete(self) -> StatusBatchQuerySet:
        """Sealed, with every child accounted for. Says nothing about the lifecycle."""
        return self.filter(sealed=True, finished__gte=models.F("total"))

    def with_progress(self) -> StatusBatchQuerySet:
        """Annotate ``progress_ratio``, so a changelist can order by least done first.

        Deliberately not called ``progress``: that name is the model's own property,
        which answers without a query and would refuse to be overwritten by an
        annotation.
        """
        return self.annotate(
            progress_ratio=models.Case(
                models.When(total=0, then=models.Value(1.0)),
                default=models.ExpressionWrapper(
                    models.F("finished") * 1.0 / models.F("total"),
                    output_field=models.FloatField(),
                ),
                output_field=models.FloatField(),
            )
        )
