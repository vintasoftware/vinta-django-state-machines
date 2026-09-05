"""Admin forms that carry a canvas graph alongside the row they create.

A version's change form talks to the canvas over its own endpoints: the graph is
loaded and saved on its own, next to the fields.  An **add** form cannot — there is
no version yet for those endpoints to hang off — so there the document rides along in
a hidden field and is applied once the row it belongs to exists.

That is what makes a machine and its first version one form instead of three visits:
fill the fields, draw the graph, save once.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from vinta_state_machines.editor import capability_errors, check_editor_machine
from vinta_state_machines.models import StateMachine, StateMachineVersion
from vinta_state_machines.scopes import get_default_scope

__all__ = [
    "GRAPH_FIELD",
    "StateMachineVersionAddForm",
    "StateMachineWithVersionForm",
]

#: Name of the hidden field the canvas writes its document into.
GRAPH_FIELD = "graph"


class _GraphFormMixin(forms.ModelForm):  # type: ignore[type-arg]
    """Adds the hidden field the canvas mirrors itself into, and validates it.

    The document is checked with :func:`check_editor_machine` while the form can
    still be redisplayed.  Applying it needs a saved row, which by then is too late
    to tell anybody their transition has no trigger.

    The capability check is the same idea one step later.  It cannot run in
    ``clean_graph``, because which scope's policy applies depends on a *sibling*
    field -- the scope on one of these forms, the machine on the other -- and per
    field cleaning has no promised order.  So it runs in :meth:`clean`, where both
    halves are cleaned, and lands its errors on the graph field anyway, next to the
    canvas that drew it.
    """

    #: Whoever is filling the form in, set by the admin.  Their bypass permission, if
    #: they have one, is what lets staff draw a graph a tenant could not have drawn.
    actor: Any = None

    graph = forms.JSONField(
        required=False,
        widget=forms.HiddenInput(attrs={"data-dsm-graph": ""}),
        label=_("graph"),
    )

    def clean_graph(self) -> Any:
        document = self.cleaned_data.get(GRAPH_FIELD)
        if not document:
            return None
        errors = check_editor_machine(document)
        if errors:
            raise forms.ValidationError(errors)
        return document

    def capability_scope(self) -> Any:
        """Whose policy governs the graph this form carries."""
        return None

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        document = cleaned.get(GRAPH_FIELD)
        if document:
            for error in capability_errors(
                document, scope=self.capability_scope(), actor=self.actor
            ):
                self.add_error(GRAPH_FIELD, error)
        return cleaned


class StateMachineWithVersionForm(_GraphFormMixin):
    """Creates a machine, the draft version its graph lives in, and that graph.

    A machine on its own governs nothing: the states and transitions are all on a
    version, so a machine created without one is a row somebody has to come back to.
    The version is always a draft — publishing is a decision taken against a graph
    that exists, from the changelist action.
    """

    version = forms.CharField(
        label=_("initial version"),
        max_length=50,
        initial="1",
        help_text=_("Label the first version is filed under, e.g. '1', '2024.1', 'v3'."),
    )
    notes = forms.CharField(
        label=_("version notes"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("Kept on the version, not on the machine."),
    )

    class Meta:
        model = StateMachine
        fields = (
            "key",
            "entity_type",
            "status_field",
            "scope",
            "name",
            "description",
            "author",
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # A machine with no tenant is the fallback machine every tenant without one
        # of its own uses -- the common case, and the one a project that has never
        # created a scope is in. Leaving the select empty means that, rather than
        # meaning "fill this in first".
        scope = self.fields["scope"]
        scope.required = False
        scope.empty_label = _("Global — the fallback for every tenant")  # type: ignore[attr-defined]
        scope.help_text = _(
            "The tenant this machine is specific to. Leave it empty for the global one."
        )

    def clean_scope(self) -> Any:
        return self.cleaned_data.get("scope") or get_default_scope()

    def capability_scope(self) -> Any:
        return self.cleaned_data.get("scope")


class StateMachineVersionAddForm(_GraphFormMixin):
    """Creates a new version of an existing machine, graph and all.

    The canvas starts from the machine's newest version rather than from nothing, so
    authoring version *n+1* is editing version *n* — which is what a new version
    almost always is.

    ``lifecycle`` is not offered: a version being drawn is a draft by definition, and
    a published one would refuse the very graph this form exists to apply.
    """

    class Meta:
        model = StateMachineVersion
        fields = ("state_machine", "version", "notes")

    def capability_scope(self) -> Any:
        machine = self.cleaned_data.get("state_machine")
        return None if machine is None else machine.scope

    def clean(self) -> dict[str, Any]:
        """Refuse a graph drawn for a different machine than the one picked.

        The canvas reloads its template whenever the machine select changes, so this
        is the select being changed back after a graph was drawn — where taking the
        document at face value would file another machine's states under this one.
        """
        cleaned: dict[str, Any] = super().clean() or {}
        machine = cleaned.get("state_machine")
        document = cleaned.get(GRAPH_FIELD)
        stamp = document.get("data") if isinstance(document, dict) else None
        drawn_for = stamp.get("machine") if isinstance(stamp, dict) else None
        if machine is not None and drawn_for is not None and drawn_for != machine.key:
            self.add_error(
                GRAPH_FIELD,
                _("This graph belongs to %(drawn)s, not %(picked)s.")
                % {"drawn": repr(drawn_for), "picked": repr(machine.key)},
            )
        return cleaned
