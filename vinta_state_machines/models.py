"""The status catalog, the versioned state machines and the status history."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from vinta_state_machines.conf import identity_model_path, scope_model_path
from vinta_state_machines.enums import (
    HookEvent,
    HookTiming,
    IdentityType,
    Lifecycle,
    ScopeType,
    StateColor,
)
from vinta_state_machines.querysets import (
    StateMachineVersionQuerySet,
    StatusTransitionQuerySet,
)
from vinta_state_machines.types import IdentitySnapshot

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


class AbstractStateMachineScope(TimeStampedModel):
    """A tenancy bucket: the slice of the world one machine governs.

    Subclasses decide what a scope *is* by implementing the :attr:`scope` property over
    whatever columns suit them -- a foreign key to an organization, a workspace slug, a
    composite -- while this class owns the one rule that holds whatever they choose:
    :attr:`scope_type` and :attr:`scope` agree, always.  Each subclass annotates its own
    :attr:`scope` with the type it actually returns; the base cannot be generic in it,
    because Django serialises a model's bases into every migration that creates it and
    a ``typing.Generic`` recorded there cannot be rebuilt on Python before 3.12.

    :attr:`scope_key` is the portable spelling of that value, and the reason it is a
    *column* rather than a property: primary keys do not cross databases, so an exported
    machine carries the key and an import resolves it back with an indexed lookup on the
    pair below.

    A scope whose ``scope_type`` is ``GLOBAL`` is the fallback every tenant resolves to
    when it has not been given a machine of its own, which is why a single tenant
    project never has to think about this table beyond the one row in it.

    Field for field this is ``vinta_audit_logs.models.AbstractAuditScope``, so a project
    running both libraries can point ``STATE_MACHINES_SCOPE_MODEL`` and
    ``AUDIT_SCOPE_MODEL`` at one model of its own.  Changing the shape here wants the
    same change there.
    """

    scope_type = models.CharField(
        _("scope type"),
        max_length=20,
        choices=ScopeType.choices,
        default=ScopeType.GLOBAL,
    )

    # The scope as a string, maintained by ``save``.  Machines are exported and imported
    # by this value, so it has to be stable for the life of the scope: definitions
    # written under an old key do not follow a new one.
    scope_key = models.CharField(_("scope key"), max_length=255, blank=True, db_index=True)

    # Human-readable name, for the admin and for exports.  A live value, not a snapshot:
    # a scope is a thing that still exists.
    label = models.CharField(_("label"), max_length=255, blank=True)

    class Meta:
        abstract = True
        ordering = ("scope_type", "scope_key")

    def __str__(self) -> str:
        return self.label or self.scope_key or str(ScopeType(self.scope_type).label)

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.validate_scope()
        self.scope_key = self.build_scope_key()
        if (
            update_fields := kwargs.get("update_fields")
        ) is not None and "scope_key" not in update_fields:
            # A partial update that moves the scope but leaves ``scope_key`` behind
            # would silently detach the scope from the machines exported under it, so
            # add the column rather than let the write proceed.
            kwargs["update_fields"] = [*update_fields, "scope_key"]
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        self.validate_scope()

    @property
    def is_global(self) -> bool:
        """Whether this is the fallback scope rather than one tenant's."""
        return self.scope_type == ScopeType.GLOBAL

    @property
    def scope(self) -> Any:
        raise NotImplementedError("Needs to be implemented on subclass")

    @scope.setter
    def scope(self, value: Any) -> None:
        raise NotImplementedError("Needs to be implemented on subclass")

    @scope.deleter
    def scope(self) -> None:
        raise NotImplementedError("Needs to be implemented on subclass")

    def build_scope_key(self) -> str:
        """Return the portable string form of this scope.

        Must be stable for the life of the scope and unique among scopes of the same
        :attr:`scope_type`.

        Returns:
            The key, or ``""`` for the global scope.
        """
        raise NotImplementedError("Needs to be implemented on subclass")

    def validate_scope(self) -> None:
        """Reject a row whose scope value and scope type disagree.

        Checked against the *final* state rather than against what changed, so insert
        and update run the identical rule and a partial update cannot slip a mismatch
        through by touching only one of the two fields.

        This is a convenience, not the guarantee: ``save`` is bypassed by
        ``bulk_create`` and ``QuerySet.update``, so concrete subclasses are expected to
        carry a ``CheckConstraint`` saying the same thing.
        """
        if self.is_global is not (self.scope is None):
            raise ValueError("The scope value and scope type fields do not match")


class StateMachineScope(AbstractStateMachineScope):
    """The scope model this app ships: an opaque string.

    Enough for a project with no tenant concept -- it holds the single global row and
    nothing else -- and the default ``STATE_MACHINES_SCOPE_MODEL`` points at.  A project
    with a real boundary points the setting at its own subclass instead and gets a
    foreign key, a label, and whatever else belongs on a tenant.
    """

    # Underscore-prefixed because ``scope`` itself is the property above; this is the
    # column behind it.  Non-nullable with ``""`` as the absent value, because an empty
    # string participates in constraints and indexes where NULL would not.
    _scope = models.CharField(_("scope"), max_length=255, blank=True)

    class Meta(AbstractStateMachineScope.Meta):
        abstract = False
        verbose_name = _("state machine scope")
        verbose_name_plural = _("state machine scopes")
        swappable = "STATE_MACHINES_SCOPE_MODEL"
        constraints: ClassVar = [
            # The invariant ``validate_scope`` checks, held where ``save`` cannot reach.
            models.CheckConstraint(
                condition=(
                    models.Q(scope_type=ScopeType.GLOBAL, _scope="")
                    | ~models.Q(scope_type=ScopeType.GLOBAL) & ~models.Q(_scope="")
                ),
                name="statemachinescope_type_and_value_agree",
            ),
            models.UniqueConstraint(
                fields=("scope_type", "scope_key"),
                name="statemachinescope_unique_key_per_type",
            ),
        ]

    @property
    def scope(self) -> str | None:
        return self._scope if self._scope != "" else None

    @scope.setter
    def scope(self, value: str | None) -> None:
        self._scope = value if value is not None else ""
        self.scope_type = ScopeType.GLOBAL if value is None else ScopeType.SCOPED

    @scope.deleter
    def scope(self) -> None:
        self._scope = ""
        self.scope_type = ScopeType.GLOBAL

    def build_scope_key(self) -> str:
        return self._scope


# ---------------------------------------------------------------------- identities


class AbstractStateMachineIdentity(TimeStampedModel):
    """Who acted, captured as they were at the moment they acted.

    **One row per reference, not one per principal.**  The columns below are a snapshot:
    the groups, permissions and display name the actor carried when they moved a record
    or published a version, which is the question a history is asked -- not what they
    carry now.  Deduplicating rows per user would answer the wrong one.

    Not every actor is a person.  A scheduled job, an API token and an internal service
    all move records, so :attr:`user` is optional and the columns that identify the
    actor -- :attr:`identity_type`, :attr:`identity_key`, :attr:`identity_label` -- are
    always populated whether or not a row in the user table backs them.

    Those columns are also what lets a history outlive its actors.  :attr:`user` is
    ``SET_NULL`` rather than ``PROTECT``, so deleting a user (an erasure request, an
    offboarding) neither fails nor takes the history with it; what the user *was* stays
    legible afterwards.

    Field for field this is ``vinta_audit_logs.models.AbstractAuditIdentity``; see
    :class:`AbstractStateMachineScope` for why that matters.
    """

    # One of :class:`~vinta_state_machines.enums.IdentityType`, or a value the installing
    # project defines.  No ``choices``: see that enum's docstring.
    identity_type = models.CharField(_("identity type"), max_length=32, default=IdentityType.USER)

    # Stable identifier for the principal, as a string so it holds a user pk, a token id
    # or a job name equally well.  For a user this is the pk at capture time, which is
    # what keeps the row identifiable once ``user`` has been nulled out.  ``""`` for a
    # principal with no id at all -- the system acting on its own behalf.
    identity_key = models.CharField(_("identity key"), max_length=255, blank=True)

    # Human-readable name at capture time.  A snapshot, not a live lookup.
    identity_label = models.CharField(_("identity label"), max_length=255, blank=True)

    # Live link to the principal when it is a user and still exists.  Nulled on
    # deletion; the snapshot columns above carry on without it.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("user"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    # --- authorization snapshot ---
    # What the actor could do at the time.  Groups and permissions are stored by name
    # rather than by relation on purpose: an M2M would describe the actor's groups
    # *now*, would break when a Group or Permission row is deleted, and would cost extra
    # writes on a path that runs on every transition.  JSON lists of strings answer
    # "what were they allowed to do when they did this".
    is_staff = models.BooleanField(_("is staff"), default=False)
    is_superuser = models.BooleanField(_("is superuser"), default=False)
    group_names = models.JSONField(_("group names"), default=list, blank=True)
    permission_keys = models.JSONField(_("permission keys"), default=list, blank=True)

    # Whatever else the project captured about this principal -- a membership role, a
    # token's scopes, the tenant it was restricted to.  Here rather than in project
    # columns so a project can extend the snapshot without swapping the model out; a
    # project that wants real columns swaps it out and gets both.
    metadata = models.JSONField(_("metadata"), default=dict, blank=True)

    class Meta:
        abstract = True
        ordering = ("-created_at", "-pk")
        indexes: ClassVar = [
            models.Index(fields=("identity_type", "identity_key")),
        ]

    def __str__(self) -> str:
        return self.identity_label or f"{self.identity_type}:{self.identity_key}"

    @property
    def is_system(self) -> bool:
        """Whether this records the system acting on its own behalf."""
        return self.identity_type == IdentityType.SYSTEM

    @classmethod
    def from_snapshot(cls, snapshot: IdentitySnapshot) -> AbstractStateMachineIdentity:
        """Build an *unsaved* row from a portable snapshot.

        The counterpart of :meth:`AbstractStateMachineScope.build_scope_key`: the model
        owns its own construction, so a project that swaps in extra columns fills them
        by overriding this rather than by configuring a builder somewhere else::

            @classmethod
            def from_snapshot(cls, snapshot):
                row = super().from_snapshot(snapshot)
                row.department = snapshot.metadata.get("department", "")
                return row

        ``metadata`` is passed through verbatim, so a subclass is free to promote keys
        out of it into real columns and leave the rest where it is.
        """
        return cls(
            identity_type=snapshot.identity_type,
            identity_key=snapshot.identity_key,
            identity_label=snapshot.identity_label,
            user_id=snapshot.user_id,
            is_staff=snapshot.is_staff,
            is_superuser=snapshot.is_superuser,
            group_names=list(snapshot.group_names),
            permission_keys=list(snapshot.permission_keys),
            metadata=dict(snapshot.metadata),
        )

    def to_snapshot(self) -> IdentitySnapshot:
        """The portable form of this row, for re-recording the same actor elsewhere."""
        return IdentitySnapshot(
            identity_type=self.identity_type,
            identity_key=self.identity_key,
            identity_label=self.identity_label,
            user_id=self.user_id,
            is_staff=self.is_staff,
            is_superuser=self.is_superuser,
            group_names=list(self.group_names or []),
            permission_keys=list(self.permission_keys or []),
            metadata=dict(self.metadata or {}),
        )


class StateMachineIdentity(AbstractStateMachineIdentity):
    """The identity model this app ships.  See :class:`AbstractStateMachineIdentity`."""

    class Meta(AbstractStateMachineIdentity.Meta):
        abstract = False
        verbose_name = _("state machine identity")
        verbose_name_plural = _("state machine identities")
        swappable = "STATE_MACHINES_IDENTITY_MODEL"


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
        on_delete=models.PROTECT,
        related_name="state_machines",
        help_text=_(
            "The tenant this machine is specific to. The global scope means it is the "
            "fallback machine, used by every tenant not given one of its own."
        ),
    )
    name = models.CharField(_("name"), max_length=200)
    description = models.TextField(_("description"), blank=True)
    author = models.ForeignKey(
        identity_model_path(),
        verbose_name=_("author"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="authored_state_machines",
        help_text=_("Who created this machine, as they were at the time."),
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
            # One constraint per rule.  The global machine is a scope *row* rather than
            # a NULL, so there is no second, partial constraint here to cover the NULL
            # that would not compare equal to itself.
            models.UniqueConstraint(
                fields=("key", "scope"),
                name="statemachine_unique_key_per_scope",
            ),
            models.UniqueConstraint(
                fields=("entity_type", "status_field", "scope"),
                name="statemachine_one_per_entity_field_per_scope",
            ),
        ]

    def __str__(self) -> str:
        return self.key

    def latest_version(self) -> StateMachineVersion | None:
        """The most recently created version, whatever its lifecycle.

        What the canvas on this machine's change form opens on, and what the label of
        the next version is bumped from.  Deliberately not ``default_version``: an
        in-flight draft is the newest picture of the flow even though nothing pins it
        yet.  Deliberately not
        :func:`~vinta_state_machines.editor.editor_machine_template`'s "newest version
        with anything on it" either -- that one is seeding a canvas and an empty draft
        would defeat it, while this one has to agree with the label the next version
        gets and with the stamp that catches a stale canvas.

        Returns:
            The version, or ``None`` for a machine that has none yet.
        """
        return self.versions.order_by("-created_at", "-pk").first()

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
        identity_model_path(),
        verbose_name=_("author"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="authored_state_machine_versions",
        help_text=_("Who published this version, as they were at the time."),
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
        on_delete=models.PROTECT,
        related_name="+",
        help_text=_(
            "Denormalised from the machine that authorised the move, so a tenant's "
            "history is one indexed filter away."
        ),
    )
    scope_key = models.CharField(
        _("scope key"),
        max_length=255,
        blank=True,
        help_text=_(
            "The scope's portable key, copied onto the row so the browse index below "
            "stands on its own and a history export needs no join."
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
        identity_model_path(),
        verbose_name=_("actor"),
        on_delete=models.PROTECT,
        related_name="status_transitions",
        help_text=_(
            "Who moved the record, as they were at the time. A move nobody was behind "
            "carries a system identity rather than nothing."
        ),
    )
    actor_type = models.CharField(_("actor type"), max_length=32)
    actor_key = models.CharField(_("actor key"), max_length=255, blank=True)
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
            # PROTECT above and these indexes are what make the log usable per tenant:
            # a history outlives the tenant, and is queryable without a scan.  They key
            # on the denormalised copies rather than the foreign keys, so neither needs
            # a join to be used.
            models.Index(
                fields=("scope_key", "-created_at"),
                name="statustransition_scope_idx",
            ),
            models.Index(
                fields=("scope_key", "actor_type", "actor_key", "-created_at"),
                name="statustransition_actor_idx",
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
