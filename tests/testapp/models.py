"""Models exercising the two shapes the app supports."""

from __future__ import annotations

from django.db import models

from vinta_state_machines.fields import (
    StateMachineMixin,
    StateMachineVersionField,
    StatusKeyField,
)
from vinta_state_machines.guards import guard_callable


class Risk(StateMachineMixin, models.Model):
    """The common shape: one governed status, using the default field names."""

    title = models.CharField(max_length=200)
    amount = models.IntegerField(default=0)
    owner_id = models.IntegerField(null=True, blank=True)

    status_key = StatusKeyField(machine="risk.status")
    status_machine_version = StateMachineVersionField()

    class Meta:
        app_label = "testapp"

    def __str__(self) -> str:
        return self.title

    @guard_callable
    def is_large(self) -> bool:
        return self.amount > 1000

    @property
    def has_owner(self) -> bool:
        return self.owner_id is not None


class Roadmap(StateMachineMixin, models.Model):
    """Two independently governed statuses on one model."""

    title = models.CharField(max_length=200)

    status_key = StatusKeyField(machine="roadmap.status")
    status_machine_version = StateMachineVersionField()

    engagement_status_key = StatusKeyField(
        machine="roadmap.engagement_status",
        version_field="engagement_machine_version",
    )
    engagement_machine_version = StateMachineVersionField()

    class Meta:
        app_label = "testapp"


class Unpinned(models.Model):
    """A status field that opts out of autopinning."""

    status_key = StatusKeyField(machine="risk.status", autopin=False)
    status_machine_version = StateMachineVersionField()

    class Meta:
        app_label = "testapp"
