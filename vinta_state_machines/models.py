"""The status catalog, the versioned state machines and the status history."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from vinta_state_machines.conf import scope_model_path
from vinta_state_machines.enums import HookEvent, HookTiming, Lifecycle, StateColor
from vinta_state_machines.querysets import (
    StateMachineVersionQuerySet,
    StatusTransitionQuerySet,
)

if TYPE_CHECKING:
    from vinta_state_machines.graph import VersionGraph

KEY_VALIDATOR_MESSAGE = _(
    "Keys are stable identifiers: use lowercase letters, digits, '.', '_' and '-'."
)
KEY_REGEX = r"^[a-z0-9][a-z0-9._-]*$"


class TimeStampedModel(models.Model):
    """Adds the ``created_at`` / ``modified_at`` pair shared by every catalog table."""

    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)
    modified_at = models.DateTimeField(_("modified at"), auto_now=True, db_index=True)

    class Meta:
        abstract = True


# ----------------------------------------------------------------------- tenancy


class StateMachineScope(TimeStampedModel):
    """A tenancy bucket: the slice of the world one machine governs.

    Swappable through ``STATE_MACHINES_SCOPE_MODEL``, so a project can point it at its
    own tenant model — an organization, a workspace — and get a real foreign key with
    real cascade instead of a loose string.  The default model below is enough on its
    own: a stable key and a name.

    A machine with no scope is *global*: it governs every tenant that has not been given
    one of its own.  Single tenant installs never populate this table.

    A swapped in model must supply the two members below, which is what keeps an
    exported machine portable across databases: primary keys do not travel, keys do.
    """

    key = models.CharField(
        _("key"),
        max_length=150,
        unique=True,
        validators=[RegexValidator(KEY_REGEX, KEY_VALIDATOR_MESSAGE)],
        help_text=_("Stable key, e.g. 'org.acme'. Travels with an exported machine."),
    )
    name = models.CharField(_("name"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("state machine scope")
        verbose_name_plural = _("state machine scopes")
        ordering = ("key",)
        swappable = "STATE_MACHINES_SCOPE_MODEL"

    def __str__(self) -> str:
        return self.key

    @property
    def scope_key(self) -> str:
        """The portable identifier for this scope."""
        return self.key

    @classmethod
    def from_scope_key(cls, key: str) -> StateMachineScope | None:
        """Resolve a :attr:`scope_key` back to a row, or ``None`` when it is unknown."""
        return cls.objects.filter(key=key).first()


# --------------------------------------------------------------------------- catalog


class StatusDefinition(TimeStampedModel):
    """The shared, cross-database vocabulary of status values.

    One row per ``(entity_type, status_field, key)``.  Status bearing records store the
    ``key`` as a soft reference instead of an inline enum, so the vocabulary is stable,
    unversioned and additive.  Presentation and ``is_initial`` / ``is_terminal`` are
    *not* here: those are version specific and live on :class:`StateMachineState`.
    """

    entity_type = models.CharField(
        _("entity type"),
        max_length=100,
        db_index=True,
        help_text=_("Which entity this status belongs to, e.g. 'risk' or 'invoice'."),
    )
    status_field = models.CharField(
        _("status field"),
        max_length=100,
        default="status",
        help_text=_("Column this vocabulary governs, e.g. 'status', 'engagement_status'."),
    )
    key = models.CharField(
        _("key"),
        max_length=100,
        help_text=_("Stable key, unique per (entity type, status field)."),
    )
    name = models.CharField(_("name"), max_length=200)
    description = models.TextField(_("description"), blank=True)

    class Meta:
        verbose_name = _("status definition")
        verbose_name_plural = _("status definitions")
        ordering = ("entity_type", "status_field", "key")
        constraints = [
            models.UniqueConstraint(
                fields=("entity_type", "status_field", "key"),
                name="statusdefinition_unique_key_per_field",
            )
        ]

    def __str__(self) -> str:
        return f"{self.entity_type}.{self.status_field}:{self.key}"


class ActionType(TimeStampedModel):
    """The shared action / event vocabulary.

    The verb that *drives* a transition and the action *recorded* when something happens
    come from this single catalog, defined once rather than twice.
    """

    key = models.CharField(
        _("key"),
        max_length=150,
        unique=True,
        help_text=_("Stable key, e.g. 'finding.accept' or 'policy.publish'."),
    )
    name = models.CharField(_("name"), max_length=200)
    domain = models.CharField(
        _("domain"),
        max_length=100,
        blank=True,
        db_index=True,
        help_text=_("Optional grouping, e.g. 'compliance', 'billing', 'identity'."),
    )
    description = models.TextField(_("description"), blank=True)

    class Meta:
        verbose_name = _("action type")
        verbose_name_plural = _("action types")
        ordering = ("domain", "key")

    def __str__(self) -> str:
        return self.key


# -------------------------------------------------------------------- state machines


class StateMachine(TimeStampedModel):
    """Governs exactly one ``(entity_type, status_field)`` pair.

    The machine itself holds no graph: every graph is an immutable
    :class:`StateMachineVersion`.  ``default_version`` is what new records pin.
    """

    key = models.CharField(
        _("key"),
        max_length=150,
        db_index=True,
        help_text=_("Stable key referenced from model fields, e.g. 'risk.status'."),
    )
    entity_type = models.CharField(_("entity type"), max_length=100, db_index=True)
    status_field = models.CharField(_("status field"), max_length=100, default="status")
    scope = models.ForeignKey(
        scope_model_path(),
        verbose_name=_("scope"),
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="state_machines",
        help_text=_(
            "The tenant this machine is specific to. Null means it is the global "
            "machine, used by every tenant that has not been given its own."
        ),
    )
    name = models.CharField(_("name"), max_length=200)
    description = models.TextField(_("description"), blank=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("author"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="authored_state_machines",
    )
    default_version = models.ForeignKey(
        "state_machines.StateMachineVersion",
        verbose_name=_("default version"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="default_for",
        help_text=_("The version new records pin. Publishing never migrates old rows."),
    )

    class Meta:
        verbose_name = _("state machine")
        verbose_name_plural = _("state machines")
        ordering = ("key",)
        constraints = [
            # Two constraints per rule, because a NULL scope (the global machine) does
            # not compare equal to itself and would slip past a single one -- the same
            # shape StateMachineTransition uses for its nullable source.
            models.UniqueConstraint(
                fields=("key", "scope"),
                condition=models.Q(scope__isnull=False),
                name="statemachine_unique_key_per_scope",
            ),
            models.UniqueConstraint(
                fields=("key",),
                condition=models.Q(scope__isnull=True),
                name="statemachine_unique_global_key",
            ),
            models.UniqueConstraint(
                fields=("entity_type", "status_field", "scope"),
                condition=models.Q(scope__isnull=False),
                name="statemachine_one_per_entity_field_per_scope",
            ),
            models.UniqueConstraint(
                fields=("entity_type", "status_field"),
                condition=models.Q(scope__isnull=True),
                name="statemachine_one_global_per_entity_field",
            ),
        ]

    def __str__(self) -> str:
        return self.key

    def statuses(self) -> models.QuerySet[StatusDefinition]:
        """The vocabulary this machine draws its states from."""
        return StatusDefinition.objects.filter(
            entity_type=self.entity_type, status_field=self.status_field
        )


class StateMachineVersion(TimeStampedModel):
    """An immutable published graph.

    Records pin the version they were created under and keep validating against that
    exact graph until they are explicitly moved.
    """

    state_machine = models.ForeignKey(
        StateMachine,
        verbose_name=_("state machine"),
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version = models.CharField(
        _("version"),
        max_length=50,
        help_text=_("Label pinned by records, e.g. '1', '2024.1', 'v3'."),
    )
    lifecycle = models.CharField(
        _("lifecycle"),
        max_length=20,
        choices=Lifecycle.choices,
        default=Lifecycle.DRAFT,
        db_index=True,
    )
    published_at = models.DateTimeField(_("published at"), null=True, blank=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("author"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="authored_state_machine_versions",
    )
    notes = models.TextField(_("notes"), blank=True)

    objects = StateMachineVersionQuerySet.as_manager()

    class Meta:
        verbose_name = _("state machine version")
        verbose_name_plural = _("state machine versions")
        ordering = ("state_machine__key", "-created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("state_machine", "version"),
                name="statemachineversion_unique_label",
            ),
            models.CheckConstraint(
                condition=models.Q(lifecycle="draft", published_at__isnull=True)
                | ~models.Q(lifecycle="draft"),
                name="statemachineversion_draft_has_no_published_at",
            ),
        ]

    def __str__(self) -> str:
        machine = self.state_machine.key if self.state_machine_id else "?"
        return f"{machine}@{self.version}"

    @property
    def is_published(self) -> bool:
        return self.lifecycle == Lifecycle.PUBLISHED

    @property
    def is_editable(self) -> bool:
        """Only drafts may have their states or transitions changed."""
        return self.lifecycle == Lifecycle.DRAFT

    def graph(self) -> VersionGraph:
        """Return the cached, read-only in-memory graph for this version.

        Passing ``self`` rather than the pk keeps any ``with_graph()`` prefetch usable
        and saves a query re-reading the row we already have.
        """
        from vinta_state_machines.graph import get_graph

        return get_graph(self)

    def mark_published(self, *, when: Any = None) -> None:
        self.lifecycle = Lifecycle.PUBLISHED
        self.published_at = when or timezone.now()


class StateMachineState(TimeStampedModel):
    """Binds a :class:`StatusDefinition` into one version as a valid state."""

    state_machine_version = models.ForeignKey(
        StateMachineVersion,
        verbose_name=_("state machine version"),
        on_delete=models.CASCADE,
        related_name="states",
    )
    status = models.ForeignKey(
        StatusDefinition,
        verbose_name=_("status"),
        on_delete=models.PROTECT,
        related_name="bound_states",
    )
    is_initial = models.BooleanField(
        _("is initial"),
        default=False,
        help_text=_("A valid starting state for a new record."),
    )
    is_terminal = models.BooleanField(
        _("is terminal"),
        default=False,
        help_text=_("No outgoing transitions are allowed from this state."),
    )
    color = models.CharField(
        _("color"),
        max_length=20,
        choices=StateColor.choices,
        default=StateColor.NEUTRAL,
    )
    order = models.PositiveIntegerField(_("order"), default=0)
    x = models.IntegerField(
        _("x"),
        default=0,
        help_text=_("Horizontal position on the version's canvas. Presentation only."),
    )
    y = models.IntegerField(
        _("y"),
        default=0,
        help_text=_("Vertical position on the version's canvas. Presentation only."),
    )

    class Meta:
        verbose_name = _("state machine state")
        verbose_name_plural = _("state machine states")
        ordering = ("state_machine_version", "order", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("state_machine_version", "status"),
                name="statemachinestate_unique_status_per_version",
            )
        ]

    def __str__(self) -> str:
        return self.status.key if self.status_id else "?"

    @property
    def status_key(self) -> str:
        return self.status.key


class StateMachineTransition(TimeStampedModel):
    """A named, guarded edge of one version's graph.

    ``from_state`` is nullable: a null source means *creation*, the edge that takes a
    brand new record into an initial state.  A state may transition to itself, and one
    pair of states may be joined by as many edges as the flow needs — which is why the
    edge, not the ``(from, to, action)`` triple, is what carries an identity.
    """

    state_machine_version = models.ForeignKey(
        StateMachineVersion,
        verbose_name=_("state machine version"),
        on_delete=models.CASCADE,
        related_name="transitions",
    )
    name = models.CharField(
        _("name"),
        max_length=200,
        help_text=_(
            "Identifies this edge among the ones leaving the same state, e.g. "
            "'approve' or 'approve_over_limit'. Unique per source state."
        ),
    )
    from_state = models.ForeignKey(
        StateMachineState,
        verbose_name=_("from state"),
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="outgoing_transitions",
        help_text=_("Null means creation: the edge into an initial state."),
    )
    to_state = models.ForeignKey(
        StateMachineState,
        verbose_name=_("to state"),
        on_delete=models.CASCADE,
        related_name="incoming_transitions",
    )
    action_type = models.ForeignKey(
        ActionType,
        verbose_name=_("action type"),
        on_delete=models.PROTECT,
        related_name="transitions",
        help_text=_("The event from the shared vocabulary that triggers this edge."),
    )
    guard = models.TextField(
        _("guard"),
        blank=True,
        help_text=_(
            "Optional boolean expression that must hold, e.g. 'obj.amount <= 1000'. "
            "A leading '@' names a guard registered with @register_guard."
        ),
    )
    required_permission = models.CharField(
        _("required permission"),
        max_length=200,
        blank=True,
        help_text=_("Optional permission the acting principal must hold."),
    )
    requires_approval = models.BooleanField(
        _("requires approval"),
        default=False,
        help_text=_("The transition needs an explicit approval to commit."),
    )
    order = models.PositiveIntegerField(
        _("order"),
        default=0,
        help_text=_(
            "Decides which edge wins when several leave the same state under the same "
            "action: the first one whose guard holds."
        ),
    )
    description = models.TextField(_("description"), blank=True)
    label_offset_x = models.IntegerField(
        _("label offset x"),
        default=0,
        help_text=_(
            "Where the edge's card sits relative to the point the canvas would pick "
            "for it. Zero keeps it on the edge. Presentation only."
        ),
    )
    label_offset_y = models.IntegerField(
        _("label offset y"),
        default=0,
        help_text=_("Vertical companion to label_offset_x. Presentation only."),
    )

    class Meta:
        verbose_name = _("state machine transition")
        verbose_name_plural = _("state machine transitions")
        ordering = ("state_machine_version", "from_state__order", "order", "pk")
        constraints = [
            # The name identifies the edge among those leaving the same state. Two
            # constraints, because a NULL source (creation) does not compare equal to
            # itself and so would slip past a single one.
            models.UniqueConstraint(
                fields=("state_machine_version", "from_state", "name"),
                condition=models.Q(from_state__isnull=False),
                name="statemachinetransition_unique_name_per_source",
            ),
            models.UniqueConstraint(
                fields=("state_machine_version", "name"),
                condition=models.Q(from_state__isnull=True),
                name="statemachinetransition_unique_creation_name",
            ),
        ]

    def __str__(self) -> str:
        source = self.from_state.status.key if self.from_state is not None else "*"
        return f"{self.name}: {source} --{self.action_type.key}--> {self.to_state.status.key}"


# --------------------------------------------------------------------------- history


class StatusTransition(TimeStampedModel):
    """Append-only, polymorphic log of every status change.

    One row per committed transition, recording what moved, from where to where, and
    which :class:`StateMachineVersion` authorized the edge.
    """

    target_type = models.ForeignKey(
        ContentType,
        verbose_name=_("target type"),
        on_delete=models.CASCADE,
        related_name="status_transitions",
    )
    target_id = models.CharField(_("target id"), max_length=64, db_index=True)
    target = GenericForeignKey("target_type", "target_id")

    status_field = models.CharField(
        _("status field"),
        max_length=100,
        default="status",
        help_text=_("Which status column changed, for entities that have more than one."),
    )
    from_status = models.ForeignKey(
        StatusDefinition,
        verbose_name=_("from status"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="transitions_out",
        help_text=_("Null on the first transition of a record."),
    )
    to_status = models.ForeignKey(
        StatusDefinition,
        verbose_name=_("to status"),
        on_delete=models.PROTECT,
        related_name="transitions_in",
    )
    state_machine_version = models.ForeignKey(
        StateMachineVersion,
        verbose_name=_("state machine version"),
        on_delete=models.PROTECT,
        related_name="authorized_transitions",
        help_text=_("The version that authorized this edge."),
    )
    scope = models.ForeignKey(
        scope_model_path(),
        verbose_name=_("scope"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text=_(
            "Denormalised from the machine that authorised the move, so a tenant's "
            "history is one indexed filter away. Null for global machines."
        ),
    )
    action_type = models.ForeignKey(
        ActionType,
        verbose_name=_("action type"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="status_transitions",
    )
    transition = models.ForeignKey(
        StateMachineTransition,
        verbose_name=_("transition"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="status_transitions",
        help_text=_(
            "The exact edge that was taken. One action may name several edges between "
            "the same pair of states, so the action alone does not identify the move."
        ),
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("actor"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="status_transitions",
        help_text=_("Null means the change was made by the system."),
    )
    comment = models.TextField(_("comment"), blank=True)
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    objects = StatusTransitionQuerySet.as_manager()

    class Meta:
        verbose_name = _("status transition")
        verbose_name_plural = _("status transitions")
        ordering = ("-created_at", "-pk")
        indexes = [
            models.Index(
                fields=("target_type", "target_id", "status_field", "-created_at"),
                name="statustransition_target_idx",
            ),
            # PROTECT above and this index are the pair that make the log usable per
            # tenant: an audit trail outlives the tenant, and is queryable without a scan.
            models.Index(
                fields=("scope", "-created_at"),
                name="statustransition_scope_idx",
            ),
        ]

    def __str__(self) -> str:
        source = self.from_status.key if self.from_status is not None else "*"
        return f"{source} -> {self.to_status.key}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self._state.adding is False:
            raise ValueError("StatusTransition rows are append-only and cannot be edited.")
        super().save(*args, **kwargs)


# ----------------------------------------------------------------------- side effects


class StateMachineHook(TimeStampedModel):
    """Binds a registered side-effect handler to a point in one version's graph.

    Handlers are plain functions registered by any app under a unique key with
    :func:`~vinta_state_machines.side_effects.register_side_effect`; this table only
    stores the *key*, so the wiring is data and travels with the version.

    A hook fires ``before`` or ``after`` the status change is committed, for one of:
    a specific transition, any transition of the version, entering a given state, or
    leaving a given state.
    """

    state_machine_version = models.ForeignKey(
        StateMachineVersion,
        verbose_name=_("state machine version"),
        on_delete=models.CASCADE,
        related_name="hooks",
    )
    handler_key = models.CharField(
        _("handler key"),
        max_length=150,
        db_index=True,
        help_text=_("Key of a function registered with @register_side_effect."),
    )
    timing = models.CharField(
        _("timing"), max_length=10, choices=HookTiming.choices, default=HookTiming.AFTER
    )
    event = models.CharField(
        _("event"), max_length=20, choices=HookEvent.choices, default=HookEvent.TRANSITION
    )
    transition = models.ForeignKey(
        StateMachineTransition,
        verbose_name=_("transition"),
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="hooks",
        help_text=_("Required when the event is 'transition'."),
    )
    state = models.ForeignKey(
        StateMachineState,
        verbose_name=_("state"),
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="hooks",
        help_text=_("Required when the event is 'enter_state' or 'leave_state'."),
    )
    params = models.JSONField(
        _("params"),
        default=dict,
        blank=True,
        help_text=_(
            "JSON parameter for this handler on this binding, stored on the relationship "
            "and handed to the function as context.params."
        ),
    )
    order = models.PositiveIntegerField(_("order"), default=0)
    is_active = models.BooleanField(_("is active"), default=True)
    on_commit = models.BooleanField(
        _("on commit"),
        default=False,
        help_text=_(
            "Defer an 'after' hook until the surrounding transaction commits. "
            "Ignored for 'before' hooks."
        ),
    )
    description = models.TextField(_("description"), blank=True)

    class Meta:
        verbose_name = _("state machine hook")
        verbose_name_plural = _("state machine hooks")
        ordering = ("state_machine_version", "timing", "order", "pk")
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(event="transition", transition__isnull=False, state__isnull=True)
                    | models.Q(event="any_transition", transition__isnull=True, state__isnull=True)
                    | models.Q(
                        event__in=("enter_state", "leave_state"),
                        transition__isnull=True,
                        state__isnull=False,
                    )
                ),
                name="statemachinehook_target_matches_event",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.timing}:{self.event} -> {self.handler_key}"
