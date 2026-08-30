# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A machine and its first version are one form.** *Add state machine* now asks for the
  machine's fields, the label of its first version and the graph together; one save creates
  the machine, files the version as a draft and applies what was drawn on the canvas. A
  machine on its own governs nothing — every state and transition lives on a version — so
  creating one without one was a row somebody had to come back to.

  The scope select may be left empty there and means the **global** machine, the fallback
  every tenant without one of its own uses, whose row is created on demand. A project that
  has never created a scope can now author its first machine without visiting another form
  first.

- **A new version starts from the previous one.** *Add state machine version* carries the
  same canvas, seeded with the machine's newest version that has anything on it — a draft
  filed and never drawn is not what anybody means by *the previous setup*. Picking a
  different machine reloads the canvas from that machine instead, and a graph stamped for one
  machine filed under another is refused rather than landing under the wrong key.

  `lifecycle` is no longer offered when adding: a version being drawn is a draft by
  definition, and a published one would refuse the very graph the form exists to apply.
  Publishing stays the changelist action it was.

  On both add forms the graph travels **with the form** — there is no row yet to hang an
  endpoint off — so there is no *Save graph* button and the form's own Save stores it. A
  refused document comes back on the form, reason above the canvas and graph still on it,
  rather than after the row has been created.

- `check_editor_machine(document)`: every reason `apply_editor_machine` would refuse a
  document, read off the document alone. The rules it cannot check are all about rows that
  are already there, and a version that has just been created has none — so for a new version
  it is the whole list, which is what lets the add forms validate before saving.
- `editor_machine_template(machine)` and `empty_editor_machine(machine=None)`: the document a
  new version starts from, and one with nothing on it. Row ids are blanked in the template,
  since a transition's id is the primary key of a row belonging to the version it came from.

### Changed

- The bundled `vinta-state-machine-editor` moves from 0.4.0 to **0.5.0**, which brings
  automatic layout. A graph that never came from the canvas — seeded by `define_machine`,
  imported, or written in a data migration — has no coordinates, so every card used to land
  on top of the others at `(0, 0)`. The editor now organizes such a graph before drawing it:
  columns left to right, one per step away from where a record enters the machine, with the
  states in a column ordered so the edges between them cross as little as possible. The new
  **Organize** button does the same on demand. On a draft the layout is offered as unsaved
  work, so *Save graph* stores the positions on the states.
- The canvas's catalog endpoints — the side effects, the actions and the guard checker — moved
  out from under a version's URL, since none of them depends on one and an add form has no
  version to hang them off. They are now `…/statemachineversion/editor/side-effects/`,
  `…/editor/actions/` and `…/editor/guard/`, served under both admins, and the permission
  they check is view permission on the model rather than on one row. The document endpoint
  is unchanged, at `…/<id>/editor/machine/`.
- The change form template's canvas markup moved into two includes,
  `admin/state_machines/_canvas.html` and `admin/state_machines/_canvas_head.html`, so both
  admins render the same thing. A project overriding
  `admin/state_machines/statemachineversion/change_form.html` still overrides it.

### Fixed

- The canvas no longer wipes the *this version is read only* note off the status line the
  moment the graph finishes loading.
- `clone_version` now carries a transition card's `label_offset_x` / `label_offset_y` across.
  A cloned version had every card back on its edge, so the canvas of version *n+1* did not
  look like the one it was cloned from.

## [0.2.0] - 2026-08-28

### Changed

- **Breaking.** Scopes and actors now follow the same shape as
  [vinta-django-audit-logs](https://github.com/vintasoftware/vinta-django-audit-logs), so a
  project running both can point one model of its own at both libraries.
- `StateMachineScope` is now `AbstractStateMachineScope` plus a concrete default. A swapped
  in model subclasses the base and implements `build_scope_key()`; the old contract of a
  `scope_key` property plus a `from_scope_key()` classmethod is gone, and `scope_key` is a
  real indexed column. `state_machines.E006` now reports a missing `build_scope_key`.
- The global scope is a **row** (`scope_type="global"`, empty `scope_key`) rather than a
  `NULL`, created on first use. `StateMachine.scope` and `StatusTransition.scope` are
  non-nullable, which collapses the four partial unique constraints on `StateMachine` to
  two plain ones.
- Every scope foreign key is `PROTECT`. `StateMachine.scope` was `CASCADE`, which meant
  deleting a tenant tried to delete its machines and was then blocked by its history.
- `StateMachine.author`, `StateMachineVersion.author` and `StatusTransition.actor` point at
  the new identity model instead of `AUTH_USER_MODEL`. `StatusTransition.actor` is
  non-nullable: a move nobody was behind carries a system identity rather than a `NULL`.
- `transition()`, `can_transition()`, `available_transitions()` and `available_actions()`
  take `actor=` instead of `user=`. `user=` still works and warns; it will be removed in
  the next minor release. Guard expressions keep their `user` context key, and gain `actor`
  alongside it.
- `SideEffectContext.actor` is the live principal the caller passed. `after` handlers read
  the recorded snapshot from `context.record.actor`.
- Building a graph costs one extra query for the machine's scope when the caller has not
  selected it. `with_graph()` and `get_graph()` already do; it is a constant, and graphs
  are cached per version.
- The migration history is squashed into a single `0001_initial`. This release predates any
  production install, so there is no upgrade path from 0.1.0 — drop the tables and migrate
  fresh.

### Added

- `STATE_MACHINES_IDENTITY_MODEL`, a swappable identity model on the `AUTH_USER_MODEL`
  pattern, with `AbstractStateMachineIdentity` to subclass and a `from_snapshot()` hook for
  filling columns of your own.
- Identities are snapshots, one row per reference: the actor's label, groups and
  permissions as they stood at that moment. Revoking a permission does not rewrite what
  already happened, and deleting the user leaves the record legible.
- `vinta_state_machines.types`: `ScopeRef`, `ScopeKey`, `IdentitySnapshot` and
  `IdentityRef`, portable value objects that hold no rows. Pass an `IdentitySnapshot` as
  the actor to record a principal this library cannot introspect — an API token, a webhook
  sender.
- `vinta_state_machines.identities`, the actor equivalent of `scopes`:
  `identity_from_user`, `system_identity`, `snapshot_for`, `resolve_identity`.
- `STATE_MACHINES["IDENTITY_RESOLVER"]` to replace how a principal becomes a snapshot, and
  `STATE_MACHINES["CAPTURE_AUTHORIZATION_SNAPSHOT"]` to skip the group and permission reads.
- `StatusTransition.scope_key`, `actor_type` and `actor_key`, denormalised so the browse
  indexes need no join, plus an index on `(scope_key, actor_type, actor_key, -created_at)`.
- `scopes.get_default_scope()`, and a `check_identity_model` system check
  (`state_machines.E007` / `E008`).

## [0.1.0] - 2026-08-25

First release under the `vinta-django-state-machines` name, superseding the
`django-state-machines` package. The **Changed** entries below are relative to that
package; if this is your first install, only **Added** applies.

### Added

- `StatusDefinition` and `ActionType`: the shared, unversioned status and action
  vocabularies, referenced everywhere by stable key.
- `StateMachine` → `StateMachineVersion` → `StateMachineState` / `StateMachineTransition`:
  a versioned graph per `(entity_type, status_field)`, with named, guarded transitions
  carrying an `action_key`, a `guard`, a `required_permission` and a `requires_approval`
  flag.
- States carry `x` and `y` positions, so a version is also the layout of its own diagram.
- Self transitions, and any number of parallel edges between one pair of states. When
  several edges share an action, the engine takes the first, by `order`, whose permission
  and guard both hold; `transition_name=` picks one explicitly.
- `StatusKeyField` and `StateMachineVersionField`, so a record stores its status as a soft
  reference and pins the version it was created under. Publishing never migrates data.
- A transition engine: `available_transitions`, `can_transition` and `transition`, plus the
  same methods as sugar on `StateMachineMixin`.
- `StatusTransition`: an append-only, polymorphic history of every status change, recording
  the exact edge that ran and the version that authorized it.
- `StateMachineHook` and the `register_side_effect` registry: functions registered under a
  unique key, wired from the catalog to run before or after a specific transition, any
  transition, entering a state or leaving a state. Each binding stores a JSON `params`
  parameter, so one handler wired to several transitions behaves differently on each.
- Authoring services: `validate_version`, `publish_version`, `clone_version`,
  `archive_version`, `rebase_record` and `define_machine`.
- Management commands `import_state_machine`, `export_state_machine` and
  `validate_state_machines`.
- Admin for the whole catalog, with publish and validate actions.
- System checks for mis-declared status fields.

- A canvas editor on the `StateMachineVersion` change form, backed by the
  `vinta-state-machine-editor` web component, which ships pre-bundled in this package's
  static files — no npm install and no build step. Drafts are editable; published and
  archived versions render read only.
- `vinta_state_machines.editor`: `to_editor_machine` and `apply_editor_machine` translate
  a version to and from the canvas document, so the editor can be embedded outside the
  admin too. Reconciling matches rows by id and updates them in place, so primary keys —
  and the history pointing at them — survive an edit.
- `StateMachineTransition.label_offset_x` / `label_offset_y`, so a transition card dragged
  off its edge stays where it was put.
- `register_side_effect` now takes `name`, `description` and `default_params`, which the
  canvas offers in its side-effect picker; the description falls back to the handler's
  docstring. `side_effect_catalog()` returns the lot.
- Admin endpoints under a version: the machine document, the side-effect and action
  catalogs, and a guard validator that answers as an expression is typed, using the same
  `validate_guard` that blocks publication.

- `StateMachineScope` and per-tenant machines. A `StateMachine` may be scoped to a tenant,
  and resolution falls back from the record's own tenant to the global machine, so a tenant
  only needs rows for the flows it actually customises. Nothing in the engine is tenant
  aware: a scoped machine simply hands out a different `StateMachineVersion`.
- `STATE_MACHINES_SCOPE_MODEL` makes the scope model **swappable**, so a project can point
  it at its own `Organization` and get a real foreign key with real cascade. A swapped in
  model must supply `scope_key` and `from_scope_key`, which keeps an exported machine
  portable between databases; `state_machines.E005` / `E006` check for them.
- `SCOPE_RESOLVER`: dotted path to `resolver(instance, config)` returning a scope, a primary
  key or a `scope_key`. `None`, the default, disables tenancy entirely.
- `StatusTransition.scope`, denormalised at write time from the machine that authorized the
  move and indexed with `created_at`, so a tenant's audit trail is one indexed filter rather
  than a scan. `PROTECT`, not `CASCADE`: an audit trail should outlive the tenant.
- `export_state_machine --scope` and a `"scope"` key in the `define_machine` definition, so
  a tenant's machine round trips between environments by key.

### Changed

- **The module is now `vinta_state_machines`** (was `django_state_machines`). The Django app
  label is unchanged (`state_machines`), so no migration is needed — update your imports and
  the entry in `INSTALLED_APPS`.
- `StateMachine.key` is unique **per scope** rather than globally, as is
  `(entity_type, status_field)`. Existing rows migrate to a null scope and keep exactly the
  uniqueness they had.
- The bundled `vinta-state-machine-editor` moves from 0.2.0 to 0.4.0: undo/redo history,
  clipboard copy, paste and duplicate, and better label placement. The document schema and
  the component's event API are unchanged.
- Reconciling a canvas document numbers a transition's `order` within the state it leaves
  rather than across the whole version. `order` only ever settles a race between edges
  leaving the same state, so the two sort identically; `define_machine` still numbers
  across the version.

[Unreleased]: https://github.com/vintasoftware/vinta-django-state-machines/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/vintasoftware/vinta-django-state-machines/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/vintasoftware/vinta-django-state-machines/releases/tag/v0.1.0
