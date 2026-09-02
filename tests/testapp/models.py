"""Models exercising the two shapes the app supports."""

from __future__ import annotations

from django.db import models

from vinta_state_machines.fields import (
    BatchReportedAtField,
    StateMachineMixin,
    StateMachineVersionField,
    StatusBatchField,
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


class ImportRun(StateMachineMixin, models.Model):
    """A parent: the record that fans work out and waits for it."""

    label = models.CharField(max_length=200)

    status_key = StatusKeyField(machine="import_run.status")
    status_machine_version = StateMachineVersionField()

    class Meta:
        app_label = "testapp"

    def __str__(self) -> str:
        return self.label


class ImportRow(StateMachineMixin, models.Model):
    """A governed child: counted by a batch, and a small state machine of its own."""

    run = models.ForeignKey(ImportRun, on_delete=models.CASCADE, related_name="rows")
    payload = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)

    batch = StatusBatchField()
    batch_reported_at = BatchReportedAtField()

    status_key = StatusKeyField(machine="import_row.status")
    status_machine_version = StateMachineVersionField()

    class Meta:
        app_label = "testapp"


class ImportNote(models.Model):
    """A second kind of child, so "one batch, several models" is actually exercised."""

    body = models.CharField(max_length=200, blank=True)

    batch = StatusBatchField()
    batch_reported_at = BatchReportedAtField()

    class Meta:
        app_label = "testapp"


class RenamedBatchChild(models.Model):
    """A child whose pair is named something else, wired explicitly."""

    etl = StatusBatchField(reported_at_field="etl_counted_at")
    etl_counted_at = BatchReportedAtField()

    class Meta:
        app_label = "testapp"
