# django-state-machines

Status is not an enum scattered across your tables. It is **data**: a shared vocabulary,
a **versioned** graph of allowed moves, and an append-only history of everything that
happened — with the version **pinned on each record**, so publishing a new graph never
migrates or invalidates a single existing row.

```python
risk = Risk.objects.create(title="Data retention")  # -> status_key "draft", pinned to v1
risk.transition("risk.assess", actor=request.user)  # -> "assessed", history row written
risk.available_actions(actor=request.user)  # -> ["risk.mitigate", "risk.reject"]
```

## Why

Most Django state-machine libraries put the graph in Python: transitions are decorated
methods, and changing the rules means a deploy. That works right up to the point where
the rules belong to the business rather than to the code — where compliance wants to add
an approval step, where two tenants need different flows, and where an auditor asks which
rules were in force when a record moved eighteen months ago.

This app answers those by making the graph a row rather than a decorator:

- **A vocabulary you can reference from anywhere.** `StatusDefinition` defines the valid
  status values for an `(entity_type, status_field)` pair. Records store a `status_key`
  soft reference, so a status travels across databases and services without a join.
- **Versions that are immutable once published.** A `StateMachineVersion` declares which
  statuses are states and which transitions are allowed. Every status-bearing row stores
  the version it was created under. Publishing v2 leaves every v1 record validating
  against v1, until you explicitly move it.
- **Guarded transitions.** Each edge carries the `action_key` that triggers it, an optional
  `guard`, a `required_permission`, and a `requires_approval` flag.
- **One action catalog.** The verb that *drives* a transition and the action you *record*
  in your audit log come from the same `ActionType` table, defined once rather than twice.
- **Side effects wired as data.** Any app registers functions under a unique key; hook rows
  in the version say when they run — before or after a specific transition, any transition,
  entering a state, or leaving a state.

`StateMachineVersion.lifecycle` (`draft | published | archived`) is the one deliberate enum
left in the design: a state machine cannot govern its own publication without infinite
recursion.

## Install

```bash
uv add vinta-django-state-machines
```

```python
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "vinta_state_machines",
    ...,
]
```

```bash
python manage.py migrate
```

## Declaring a status-bearing model

A governed status is two concrete fields: the soft reference to the catalog, and the pin
to the version whose rules apply to this row.

```python
from django.db import models
from vinta_state_machines.fields import (
    StateMachineMixin,
    StateMachineVersionField,
    StatusKeyField,
)


class Risk(StateMachineMixin, models.Model):
    title = models.CharField(max_length=200)
    amount = models.IntegerField(default=0)

    status_key = StatusKeyField(machine="risk.status")
    status_machine_version = StateMachineVersionField()
```

Both are ordinary fields, so migrations, `select_related` and type checking behave exactly
as they would on a hand-written pair. `StateMachineMixin` is optional sugar; every method
it adds is also a plain function in `vinta_state_machines.engine` that takes the instance,
which is what you want for models you do not own.

**Two statuses on one model?** Declare the pair twice. The companion field name is derived
from the status field (`status_key` → `status_machine_version`), or named explicitly:

```python
class Roadmap(StateMachineMixin, models.Model):
    status_key = StatusKeyField(machine="roadmap.status")
    status_machine_version = StateMachineVersionField()

    engagement_status_key = StatusKeyField(
        machine="roadmap.engagement_status",
        version_field="engagement_machine_version",
    )
    engagement_machine_version = StateMachineVersionField()
```

A system check flags a status field whose companion is missing, is not a foreign key, or
points at the wrong model — before it can fail at runtime.

## Defining a machine

The catalog is ordinary data, so you can build it in the admin, in a data migration, or
from a JSON file. `define_machine` is the shortest path:

```python
from vinta_state_machines.services import define_machine, publish_version

version = define_machine(
    {
        "key": "risk.status",
        "entity_type": "risk",
        "status_field": "status",
        "name": "Risk status",
        "version": "1",
        "states": [
            {"key": "draft", "name": "Draft", "is_initial": True, "x": 0, "y": 0},
            {"key": "assessed", "name": "Assessed", "x": 200, "y": 0},
            {"key": "mitigated", "name": "Mitigated", "is_terminal": True, "x": 400, "y": -80},
            {"key": "rejected", "name": "Rejected", "is_terminal": True, "x": 400, "y": 80},
        ],
        "transitions": [
            {"name": "create", "from": None, "to": "draft", "action": "risk.create"},
            {"name": "assess", "from": "draft", "to": "assessed", "action": "risk.assess"},
            {"name": "comment", "from": "assessed", "to": "assessed", "action": "risk.comment"},
            {
                "name": "mitigate",
                "from": "assessed",
                "to": "mitigated",
                "action": "risk.mitigate",
                "guard": "obj.amount <= 1000",
            },
            {
                "name": "reject",
                "from": "assessed",
                "to": "rejected",
                "action": "risk.reject",
                "required_permission": "risks.reject_risk",
                "requires_approval": True,
            },
        ],
    }
)

publish_version(version)  # validates the graph, freezes it, makes it the default
```

A `from` of `None` is the **creation edge**: the transition that takes a brand new record
into an initial state. `comment` is a **self transition** — a legal move that leaves a
state and arrives back at it, appending history without changing the status.

Each state carries `x` and `y` integers, so a version doubles as the layout of its own
diagram. The canvas positions live with the graph and travel through clone, export and
import along with everything else, which is what lets a visual editor round-trip a version
without a second store to keep in sync.

The same JSON works from the command line:

```bash
python manage.py import_state_machine risk.json --publish
```

```bash
python manage.py export_state_machine risk.status --label 1 --output risk.json
```

## Moving records

```python
from vinta_state_machines.engine import available_transitions, can_transition, transition
```

A new record pins the machine's `default_version` and starts on its initial state, so
`Risk.objects.create(title=...)` already lands somewhere valid.

```python
transition(risk, "risk.assess", actor=request.user, comment="Reviewed by security")
```

That single call, inside one transaction:

1. resolves the pinned version and refuses to run under a draft or archived one,
2. collects the edges that `action` could mean from the current status,
3. takes the first whose `required_permission` and `guard` both hold, then checks
   `requires_approval` on it,
4. runs the `before` side effects,
5. writes the new status and pins the version if it was not pinned,
6. appends a `StatusTransition` row,
7. runs the `after` side effects.

Anything that fails rolls the whole thing back. Failures raise a subclass of
`StateMachineError` — which is itself a `ValidationError`, so transition problems surface
naturally through forms and serializers:

| Exception | Raised when |
| --- | --- |
| `TransitionNotAllowed` | the version declares no such edge, or the state is terminal |
| `GuardFailed` | the edge exists but its guard did not hold |
| `PermissionDenied` | the actor lacks `required_permission` |
| `ApprovalRequired` | the edge is flagged `requires_approval` and none was passed |
| `NoStateMachineVersion` | nothing to resolve: no pin and no default version |
| `InvalidVersionState` | the pinned version is not published |
| `UnknownStatus` | the record's current status is not a state of its version |

For rendering a UI, ask for the blocked edges too and show why each one is unavailable:

```python
for option in available_transitions(risk, actor=request.user, include_blocked=True):
    render(option.name, enabled=option.allowed, tooltip=option.reason)
```

Useful keyword arguments to `transition()`:

| Argument | Effect |
| --- | --- |
| `approval=...` | satisfies `requires_approval`; recorded in the history row's metadata |
| `transition_name=...` | take exactly this edge instead of resolving by `order` |
| `metadata={...}` | free-form data stored on the history row and passed to side effects |
| `enforce_permissions=False` | for system-driven moves with no acting user |
| `allow_unpublished=True` | exercise a draft while authoring it |
| `save=False` | apply the change in memory and let the caller save |
| `record_history=False` | skip the `StatusTransition` row |

## Named edges, self transitions and parallel edges

Every transition carries a `name`, unique among the edges leaving the same state. The name
is what identifies an edge, because `(from, to, action)` no longer can:

- a state may transition **to itself**, and
- one pair of states may be joined by **as many edges as the flow needs**, including
  several that share an action.

When an action maps to several edges, the engine walks them in `order` and takes the first
whose permission *and* guard both hold. That is how you say "approve, but which path
depends on the amount" without inventing two verbs for one business action:

```python
(
    {
        "name": "approve_large",
        "from": "review",
        "to": "approved",
        "action": "invoice.approve",
        "guard": "obj.total > 1000",
        "required_permission": "invoices.approve_large",
        "requires_approval": True,
        "order": 0,
    },
)
({"name": "approve", "from": "review", "to": "approved", "action": "invoice.approve", "order": 1},)
```

If no candidate is viable, the error comes from the first one, so the message still points
at a specific edge rather than shrugging. To bypass resolution order and demand one exact
edge:

```python
transition(invoice, "invoice.approve", transition_name="approve_large", approval=sign_off)
```

`available_transitions()` lists every edge separately, each with its own `.name`, so a UI
can render two buttons for the same action with different labels and tooltips. And because
two history rows for one action would otherwise be indistinguishable, `StatusTransition`
records the exact `transition` that ran, not just the action that named it.

A self transition is an ordinary move: guards, permissions, side effects and a history row
all apply, the status simply ends up where it started. Terminal states remain terminal —
looping back to yourself still counts as an outgoing edge, and validation says so.

## Guards

A guard is either a **safe expression** or a **registered function**.

Expressions are evaluated against a restricted subset of Python — no imports, no private
names, no method calls, no statements — with `obj`, `user`, `action`, `from_status`,
`to_status` and `metadata` in scope:

```
obj.amount <= 1000 and obj.owner_id is not None
```

Guards never invoke arbitrary methods, because `obj.delete` reads exactly like an
attribute. Opt a no-argument method in when you want it evaluated like a property:

```python
from vinta_state_machines.guards import guard_callable


class Risk(models.Model):
    @guard_callable
    def is_large(self):
        return self.amount > 1000
```

For anything with real logic, register a named guard and reference it as `"@key"`:

```python
from vinta_state_machines.guards import register_guard


@register_guard("risk.within_budget")
def within_budget(obj, user, **context):
    return obj.amount <= obj.project.remaining_budget
```

Set `STATE_MACHINES = {"ALLOW_GUARD_EXPRESSIONS": False}` to require the named form
everywhere. Either way, a broken guard is caught by `validate_version` at authoring time
rather than the first time a record tries to move.

## Side effects

Register a function under a unique key from any app — put them in `side_effects.py` and
they are imported automatically:

```python
# risks/side_effects.py
from vinta_state_machines.side_effects import AbortTransition, register_side_effect


@register_side_effect("risk.notify_owner")
def notify_owner(context):
    send_mail(
        to=context.instance.owner.email,
        subject=f"{context.instance} is now {context.to_status}",
        template=context.params["template"],
    )


@register_side_effect("risk.require_evidence")
def require_evidence(context):
    if not context.instance.evidence.exists():
        raise AbortTransition("Attach evidence before assessing this risk.")
```

Then wire them from the version, which is what makes them travel with the graph rather
than with the code:

```python
StateMachineHook.objects.create(
    state_machine_version=version,
    handler_key="risk.notify_owner",
    timing="after",  # before | after
    event="enter_state",  # transition | any_transition | enter_state | leave_state
    state=version.states.get(status__key="mitigated"),
    on_commit=True,  # wait for the surrounding transaction to commit
    params={"template": "risk_mitigated", "cc": ["compliance@example.com"]},
)
```

`params` is a JSON parameter **stored on the relationship itself**, so one registered
function can be wired to several transitions and behave differently on each — and the
parameters travel with the version through clone, publish, export and import. The handler
reads it as `context.params`. In a definition dict it is just another key:

```python
"hooks": [
    {
        "handler": "risk.notify_owner",
        "transition": "mitigate",
        "timing": "after",
        "on_commit": True,
        "params": {"template": "risk_mitigated"},
    },
]
```

Do not confuse it with `metadata`: `params` is authoring-time configuration attached to the
binding, while `metadata` is per-move data supplied by whoever called `transition()`. A
handler usually reads both.

`event` picks what the hook is bound to:

| `event` | Fires for | Names |
| --- | --- | --- |
| `transition` | one specific edge | `transition`, by name — plus `from` when the same name leaves more than one state |
| `any_transition` | every edge of the version | — |
| `enter_state` | every edge arriving at a state | `state` |
| `leave_state` | every edge departing a state | `state` |

Within one timing, hooks run **leave → transition → enter**, and `order` breaks ties on the
same binding. The two timings mirror each other, so a `before`/`after` pair on the same
binding always brackets the change:

```
before-leave → before-transition → before-enter
                                       ↓
                           status written, history appended
                                       ↓
 after-leave → after-transition → after-enter
```

Every handler takes one `SideEffectContext`:

| Attribute | |
| --- | --- |
| `instance`, `field_name`, `status_field` | what is changing |
| `from_status`, `to_status`, `action` | the move; `from_status` is `None` on creation |
| `version`, `graph`, `transition` | the authorizing version and the edge |
| `timing`, `event`, `hook` | which binding fired |
| `actor` | the live principal the caller passed, or `None` for the system |
| `params` | the JSON parameter stored on this binding, verbatim |
| `metadata` | whatever the caller passed to `transition()`, for this move |
| `record` | the history row — set for `after` handlers only |

The engine writes only the status columns. A handler that also changes the record says so:

```python
@register_side_effect("risk.stamp_closed_at")
def stamp_closed_at(context):
    context.instance.closed_at = timezone.now()
    context.touch("closed_at")
```

Everything runs inside the transition's transaction. A `before` handler raising
`AbortTransition` vetoes the move cleanly; any other exception rolls it back. Use
`on_commit=True` for `after` handlers that reach outside the database — emails, webhooks,
queued jobs — so they never fire for a transaction that ends up rolling back.

`validate_version` refuses to publish a version whose hooks name a handler no installed app
registers, so a typo is caught at publish time rather than at 3am.

## Versioning

This is the part that pays for itself. Records pin, so publishing is safe:

```python
from vinta_state_machines.services import archive_version, clone_version, publish_version

v2 = clone_version(machine.default_version, "2")  # deep copy into a fresh draft
v2.transitions.create(...)  # edit the draft freely
publish_version(v2)  # new records pin v2
```

Existing records keep validating against v1. Nothing was migrated; nothing was invalidated.
When you *do* want a record to move forward, that is an explicit, auditable act:

```python
from vinta_state_machines.services import rebase_record

rebase_record(risk, v2, map_status={"draft": "new"})
```

`archive_version` retires a version without breaking the records that pinned it — a pinned
version is protected by the database, so it always outlives its rows.

`validate_version` checks a graph as a whole and separates what blocks publication from
what merely deserves a look:

- **Errors** — no states, no initial state, a transition or hook pointing into another
  version, an outgoing edge from a terminal state, a creation edge into a non-initial
  state, an unusable guard, a hook naming an unregistered handler.
- **Warnings** — an unreachable state, a dead end that is not marked terminal.

```bash
python manage.py validate_state_machines --fail-on-warning
python manage.py validate_state_machines --list-side-effects
```

## Editing a graph on a canvas

The admin embeds
[`vinta-state-machine-editor`](https://www.npmjs.com/package/vinta-state-machine-editor), a
web component that draws a graph as a pan/zoom canvas: states as cards you can drag and
colour, transitions as edges you draw between them, and the side effects around both as
ordered lists you fill from a dialog.

Nothing is needed to switch it on. The component ships pre-bundled inside this package, so
there is no npm install and no build step — only `django.contrib.staticfiles` and a
`STATIC_URL`, which any Django project already has.

The same canvas sits on both change forms, and which one you want depends on whether you are
thinking about versions at all:

| | **A `StateMachine`** | **A `StateMachineVersion`** |
| --- | --- | --- |
| Opens on | the machine's latest version | that version |
| Saving | publishes a **new** version and makes it the default | edits that version in place |
| Editable when | you can change the machine | the version is still a draft |

A version's canvas edits **drafts**. A published or archived version renders read only,
exactly as its form fields do, because records pin it and its graph can no longer change.

### Editing the machine, and letting the version follow

Open a `StateMachine` and you get its latest graph, whatever its lifecycle. Change it, press
**Save and publish a new version**, and that is exactly what happens: a fresh version is
written, validated, published and made the default, in one transaction. Nothing that already
exists is touched, so records that pinned the old graph go on validating against precisely
the graph they pinned — versioning falls out of editing instead of being one more thing to
remember.

Three things worth knowing about that save:

- The new version is **built from the document**, not copied from the one it was drawn on.
  Hooks bound to `any_transition` are the exception: the canvas cannot draw them, so they are
  carried across explicitly and survive every revision.
- A graph that would not pass `validate_version` is **refused whole** — no half-published
  version, no draft left behind — and every reason comes back at once. Warnings do not block;
  they arrive as admin messages.
- The document remembers which version it was serialized from, so a canvas left open in one
  tab while another published a version is refused rather than quietly landing on top of work
  it never saw.

When you want the finer-grained path instead — revise, review, publish when ready — **Clone
selected versions as new drafts** on the version changelist deep copies a version's states,
transitions, hooks and layout into a new draft and leaves the version it copied entirely
alone. Its label comes from `next_version_label`, the same bump the machine's canvas uses.

### Creating a machine and its first version together

A machine on its own governs nothing — every state and transition lives on a version — so
**Add state machine** asks for the machine's fields, the label of its first version, and the
graph, all on one form. One save creates the machine, files the version as a draft and
applies what was drawn.

The scope select may be left empty there: an empty scope is the **global** machine, the
fallback every tenant without one of its own uses, and its row is created on demand. A
project that has never created a scope can therefore author its first machine without
visiting another form first.

### Starting a new version from the previous one

**Add state machine version** carries the same canvas, seeded with the machine's newest
non-empty version. Authoring version *n+1* is editing version *n*, which is what a new
version almost always is — the same move [`clone_version`](#versioning) makes from code, and
the graph is still yours to change before anything is saved.

Picking a different machine reloads the canvas from that machine instead. The document is
stamped with the machine it was drawn for, so a graph and a machine that no longer agree is
refused rather than filed under the wrong key.

New versions are always drafts: `lifecycle` is not on the add form, because a published
version would refuse the very graph the form exists to apply. Publish from the changelist
action once the graph is what you want.

On these two forms the graph travels **with the form** rather than on its own endpoint —
there is no row yet to hang one off — so there is no *Save graph* button, and the form's own
Save is what stores it. A document the server refuses comes back on the form with the reason
above the canvas and the graph still on it, rather than after the row has been created.

### Cards nobody has placed

A graph that never came from the canvas — one seeded by `define_machine`, imported, or
written in a data migration — usually has no coordinates, so every card sits at `(0, 0)`.
The editor notices and **organizes the layout** before drawing: columns left to right, one
per step away from where a record enters the machine, with the states in a column ordered so
the edges between them cross as little as possible. Pressing **Organize** in the toolbar does
the same thing on demand, whatever the graph looks like.

The layout is a change like any other, so on a draft it is offered as unsaved work: press
**Save graph** and the positions are stored on the states, and the next visit draws exactly
what you left.

What each side calls things:

| On the canvas | In the database |
| --- | --- |
| a state's `id` | the `StatusDefinition` key it binds |
| ▶ Initial / ◉ Final on a card | `is_initial` / `is_terminal` |
| an edge from the start dot | a transition with `from_state=None` |
| an edge's trigger | its `ActionType`, from the shared vocabulary |
| the four lists on a card | `enter_state` / `leave_state` hooks, before and after |
| the two lists on an edge | `transition` hooks, before and after |
| where things sit | `x` / `y` on a state, `label_offset_*` on an edge |

Ordering is positional throughout: dragging a card above another renumbers `order`, both
for the edges leaving one state and for the side effects in one list.

Two things stay off the canvas by design. Hooks bound to `any_transition` belong to the
version rather than to any one card, so the editor never sees them and never disturbs
them — edit those in the inline below. And a state drawn on the canvas arrives with a
generated id; the server gives it a real vocabulary key, slugified from its name, and hands
the saved document back so the canvas picks the key up.

Handlers can describe themselves for the side-effect picker:

```python
@register_side_effect(
    "risk.notify_owner",
    name="Notify the owner",
    description="Emails whoever owns the risk.",
    default_params={"template": "risk/notify.txt"},
)
def notify_owner(context): ...
```

Guard expressions are checked by the server as they are typed, through the same
`validate_guard` that blocks publication, so a broken guard is caught while it is being
written rather than at publish time.

To put the canvas somewhere other than the admin, the translation is a pair of plain
functions:

```python
from vinta_state_machines.editor import apply_editor_machine, to_editor_machine

document = to_editor_machine(version)  # -> the JSON the component reads
apply_editor_machine(version, document)  # <- reconcile a posted one, in one transaction
```

`apply_editor_machine` matches rows by id and updates them in place, so primary keys — and
the history pointing at them — survive an edit. It raises `EditorPayloadError`, which
carries **every** problem it found rather than only the first, and rolls the whole document
back if any of them fire.

`publish_editor_machine(machine, document)` is the machine-level save behind the canvas
above: it lands a document as the machine's next published version in one transaction, and
returns that version together with the validation warnings that did not block it. It raises
the same `EditorPayloadError` for a stale, unreconcilable or unpublishable document, and
writes nothing in any of those cases.

Three more, for a canvas on a form rather than on a saved row:

```python
from vinta_state_machines.editor import (
    check_editor_machine,  # every reason a document would be refused, without saving
    editor_machine_template,  # the document a new version of a machine starts from
    empty_editor_machine,  # a canvas with nothing on it
)

errors = check_editor_machine(document)  # -> [] when it would apply cleanly
```

`check_editor_machine` reads the document alone. The rules it cannot check are all about
rows that are already there — an edge recorded history points at — and a version that has
just been created has none, so for a **new** version it is the whole list. That is what lets
the add forms refuse a graph while the person who drew it can still fix it.

### The canvas in the admin's language

Every word on the canvas — the toolbar, the cards, both dialogs, and the notes under the
save button — goes through Django's own translation machinery. The component ships English
and knows nothing about locales, so the admin hands it a set of strings built from the
`djangojs` catalog for whatever language the request settled on.

Nothing is needed to switch it on, and a project with no translations reads exactly the
English it read before. To translate it, write a `djangojs` catalog for the language and
put it on `LOCALE_PATHS`, where the rest of your JavaScript translations already live. The
msgids are the English strings themselves, marked in one file you can extract from:

```bash
xgettext --language=JavaScript --from-code=UTF-8 -o locale/djangojs.pot \
  .venv/lib/python3.12/site-packages/vinta_state_machines/static/vinta_state_machines/editor-strings.js
```

Three things worth knowing:

- **Plurals come out right.** The count belongs to the browser — how many side effects a
  chip is showing, how many items a parameter list has — so the catalog is served as
  JavaScript, carrying the language's own `Plural-Forms` rule, from `editor/i18n.js` beside
  the other canvas endpoints. Russian gets its three forms; a rendered blob of singulars
  and plurals could not have given them.
- **Four of the strings are data rather than labels.** `State 1`, `transition`, `create`
  and the `copy` suffix are the names a newly drawn element is *born with*, and they are
  saved into the version. Translating them translates the graph somebody draws — including
  a new state's vocabulary key, which is slugified from its name. Nothing structural rides
  on them: a creation edge is one with no source, whatever it is called.
- **The catalog is this app's, plus yours.** `LOCALE_PATHS` is always merged in, and
  `editor_i18n_packages` on the ModelAdmin says whose app catalogs join it.

## Per-tenant machines

A machine may be **scoped** to a tenant, so one organization runs a stricter approval flow
than another while both share the same status vocabulary and the same reporting.

Nothing in the engine is tenant-aware: records pin a `StateMachineVersion`, and a scoped
machine simply hands out a different one. Tenancy lives entirely in *resolution*.

```python
STATE_MACHINES = {"SCOPE_RESOLVER": "myproject.tenancy.scope_for"}
```

```python
# myproject/tenancy.py
def scope_for(instance, config):
    """Which tenant governs this record. None means the global machine."""
    return getattr(instance, "organization", None)
```

Resolution is a two-step fallback — **the record's own tenant first, the global machine
second** — so a tenant only needs rows for the flows it actually customises:

```python
define_machine({"key": "risk.status", "scope": "org:acme", ...})  # acme's own rules
define_machine({"key": "risk.status", ...})                       # everyone else
```

A machine with no scope is global, which is why a single-tenant project never has to know
any of this exists.

### Pointing the scope at your own model

`StateMachineScope` is **swappable**. Subclass `AbstractStateMachineScope` over your own
tenant table and every scope foreign key becomes a real foreign key to your model:

```python
# settings.py — a top-level setting, because Meta.swappable resolves against one
STATE_MACHINES_SCOPE_MODEL = "organizations.OrganizationScope"
```

```python
from vinta_state_machines.enums import ScopeType
from vinta_state_machines.models import AbstractStateMachineScope


class OrganizationScope(AbstractStateMachineScope):
    """The adapter between the library and your own tenant table."""

    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.PROTECT)

    class Meta(AbstractStateMachineScope.Meta):
        abstract = False
        swappable = "STATE_MACHINES_SCOPE_MODEL"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(scope_type=ScopeType.GLOBAL, organization__isnull=True)
                    | ~models.Q(scope_type=ScopeType.GLOBAL) & models.Q(organization__isnull=False)
                ),
                name="scope_type_and_organization_agree",
            ),
            models.UniqueConstraint(
                fields=("scope_type", "scope_key"), name="scope_unique_key_per_type"
            ),
        ]

    @property
    def scope(self):
        return self.organization

    @scope.setter
    def scope(self, value):
        self.organization = value
        self.scope_type = ScopeType.GLOBAL if value is None else ScopeType.SCOPED

    @scope.deleter
    def scope(self):
        self.organization = None
        self.scope_type = ScopeType.GLOBAL

    def build_scope_key(self) -> str:
        """Stable, portable, and "" for the global scope."""
        return "" if self.organization_id is None else f"org:{self.organization.slug}"
```

Your own model stays a plain project model — the scope row is the adapter, so nothing
from this library lands on `Organization` itself.

`build_scope_key` is the contract, and `manage.py check` enforces it along with the base
class (`state_machines.E005` / `E006`). It exists because primary keys do not travel
between databases but keys do — it is what lets `export_state_machine --scope` write a
machine in staging and `import_state_machine` read it back in production.

The global scope is a **row**, not a `NULL`: `scope_type="global"` with an empty
`scope_key`. It is created on first use, so there is nothing to seed. That is what keeps
resolution a single code path and lets `StateMachine` carry one unique constraint per
rule instead of a pair — a `NULL` does not compare equal to itself, so it slips past a
plain unique index.

Like every swappable model this is an **install-time, one-way decision**: make it before you
have data, the way you would `AUTH_USER_MODEL`.

## Actors and identities

Every entry point takes an `actor`: whoever is trying to move the record.

```python
transition(risk, "risk.assess", actor=request.user)
```

The engine keeps that principal **live** while it is deciding — permissions and guards
need something that can answer `has_perm` — and only **snapshots** it when it writes the
move down. The history row points at an identity row holding the actor's label, groups
and permissions *as they were at that moment*:

```python
record = transition(risk, "risk.reject", actor=request.user)
record.actor.identity_label  # "ana", as it read then
record.actor.permission_keys  # what she was allowed to do then
record.actor_type, record.actor_key  # denormalised onto the row, for the index
```

That is one identity row **per move**, not per person: two transitions a month apart are
two snapshots, because what the actor could do between them may differ. Revoking a
permission does not rewrite what already happened, and deleting the user nulls
`identity.user` while leaving the rest legible.

Not every actor is a person. `actor=None` records a system identity — a real row, not a
`NULL`, so every history row has an actor. For a principal this library cannot introspect
— an API token, a webhook sender — pass a snapshot directly:

```python
from vinta_state_machines.enums import IdentityType
from vinta_state_machines.types import IdentitySnapshot

transition(
    risk,
    "risk.assess",
    actor=IdentitySnapshot(
        identity_type=IdentityType.SERVICE,
        identity_key="billing-worker",
        identity_label="Billing worker",
    ),
)
```

`StateMachine.author` and `StateMachineVersion.author` are the same kind of reference, so
`publish_version(version, author=request.user)` records who published a graph and what
they were allowed to do at the time.

### Pointing the identity at your own model

`StateMachineIdentity` is swappable on the same terms as the scope:

```python
STATE_MACHINES_IDENTITY_MODEL = "accounts.PrincipalIdentity"
```

```python
from vinta_state_machines.models import AbstractStateMachineIdentity


class PrincipalIdentity(AbstractStateMachineIdentity):
    department = models.CharField(max_length=64, blank=True)

    class Meta(AbstractStateMachineIdentity.Meta):
        abstract = False
        swappable = "STATE_MACHINES_IDENTITY_MODEL"

    @classmethod
    def from_snapshot(cls, snapshot):
        row = super().from_snapshot(snapshot)
        row.department = row.metadata.pop("department", "")
        return row
```

`from_snapshot` is the hook: the library hands over a portable snapshot and your model
decides what to promote into real columns. Whatever it leaves behind stays in `metadata`.

Both models are shaped field-for-field like their counterparts in
[vinta-django-audit-logs](https://github.com/vintasoftware/vinta-django-audit-logs), so a
project running both can point `STATE_MACHINES_SCOPE_MODEL` and `AUDIT_SCOPE_MODEL` at
one model of its own.

### What the scope buys on the history table

`StatusTransition` carries the scope too, denormalised from the machine that authorized the
move — both as a foreign key and as a `scope_key` string, so the browse indexes stand on
their own and a tenant's audit trail is one indexed filter away rather than a scan:

```python
StatusTransition.objects.filter(scope_key="org:acme", created_at__gte=start)
StatusTransition.objects.filter(  # everything one person did, in one tenant
    scope_key="org:acme", actor_type="user", actor_key=str(user.pk)
)
```

Every scope and actor foreign key is `PROTECT`. An audit trail should outlive the tenant
and the principal it describes, so deleting either forces an explicit archival step rather
than quietly destroying history.

## History

Every committed transition appends one `StatusTransition` row, recording what moved, from
where to where, who did it, **which edge ran**, and — crucially — **which version
authorized it**:

```python
from vinta_state_machines.models import StatusTransition

StatusTransition.objects.for_object(risk).with_related()
StatusTransition.objects.for_model(Risk).entering("mitigated")
```

Rows are append-only: editing one raises. The target is a generic foreign key, so one table
covers every status-bearing model in the project.

## Settings

All optional, all under one key:

```python
STATE_MACHINES = {
    "AUTOPIN_DEFAULT_VERSION": True,  # pin and fill the initial status on create
    "STRICT": False,  # raise instead of skipping autopin when the
    # catalog is not there yet
    "RECORD_HISTORY": True,
    "CACHE_GRAPHS": True,  # keep parsed graphs in memory
    "ALLOW_GUARD_EXPRESSIONS": True,
    "MAX_GUARD_EXPRESSION_LENGTH": 1000,
    "TRANSITIONABLE_LIFECYCLES": ("published",),
    "PERMISSION_CHECKER": None,  # dotted path to checker(actor, perm, instance)
    "SCOPE_RESOLVER": None,  # dotted path to resolver(instance, config);
    # None disables tenancy entirely
    "IDENTITY_RESOLVER": None,  # dotted path to resolver(actor) -> IdentitySnapshot
    "CAPTURE_AUTHORIZATION_SNAPSHOT": True,  # record the actor's groups and permissions
}
```

Two settings live outside that dict, because `Meta.swappable` can only resolve against a
top-level name:

```python
STATE_MACHINES_SCOPE_MODEL = "organizations.OrganizationScope"  # defaults to built-in
STATE_MACHINES_IDENTITY_MODEL = "accounts.PrincipalIdentity"  # defaults to built-in
```

Graphs are cached per version and invalidated whenever any row of that version changes.
Because primary keys are recycled between tests, set `"CACHE_GRAPHS": False` in your test
settings, or call `vinta_state_machines.graph.clear_graph_cache()` in a fixture.

## Development

```bash
uv sync --all-groups
uv run pytest
uv run mypy
uv run ruff check .
uv run pre-commit install --install-hooks --hook-type commit-msg
```

The full support matrix runs under tox:

```bash
uv run tox
```

Python 3.10–3.14, Django 5.2 and later. Django 6.0 requires Python 3.12+, which is why the
matrix pairs factors explicitly rather than taking the full product.

`static/vinta_state_machines/state-machine-editor.js` is the vendored `./bundled` build of
[`vinta-state-machine-editor`](https://github.com/vintasoftware/vinta-state-machine-editor)
(MIT, licence kept beside it). It is checked in rather than fetched so that installing this
package needs no Node toolchain. To move to a new release:

```bash
npm pack vinta-state-machine-editor@<version>   # currently 0.9.0
tar -xzf vinta-state-machine-editor-<version>.tgz
cp package/dist/bundled.js \
  vinta_state_machines/static/vinta_state_machines/state-machine-editor.js
```

A release that touches `EditorStrings` needs `editor-strings.js` looked at as well: it names
every group and key the component has, and one it does not have is ignored in silence rather
than refused. `test_every_string_the_glue_names_exists_in_the_component` checks the names
against the bundle that is actually vendored, which catches a key that was renamed but not a
group that was added.

## License

MIT. See [LICENSE](LICENSE).
