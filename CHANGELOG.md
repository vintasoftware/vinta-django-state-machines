# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The canvas draws the fan-out.** A state that waits for a batch is declared on the graph —
  `is_waiting`, `join_action`, `child_machine` and `batch_timeout` on `StateMachineState` —
  and the editor payload carries all four in `state.data`, so the canvas can badge the state,
  draw the band and link to the machine its children are governed by. A document that says
  nothing about `is_waiting` round-trips exactly as it did before.
- `counts_as` on a child machine's finished states, derived from the report bindings rather
  than stored, so the canvas never has to match on a handler key to know that a state counts
  towards its parent's batch. The bindings themselves are hidden from the `onEnter` /
  `onLeave` lanes: they are one concept in two rows, and two chips invite deleting half of it.
- `counts_as_partial`, which names the half a half-configured pair arrived carrying. The
  editor never sees a hook row, so it cannot work this out for itself; without the key a pair
  missing its leave half draws as though it were whole, and a leave half on its own does not
  draw at all. `validate_version` still refuses such a graph at publish time — this is what
  puts the same fact on the card while the person who caused it is still looking at it.
- `data-machines-url` on the canvas, and a `state-machine-fan-out` listener in the glue. The
  component announces that somebody asked to follow a fan-out and stops there, because a
  canvas draws one machine and a fan-out crosses into another; this is the admin answering,
  by searching the machine list for the child machine's key. It asks before leaving a graph
  with unsaved changes.
- `validate_version` refuses a waiting state whose version declares no edge under its join
  action. The batch would complete, fire, find nothing, and leave the record waiting for
  good — a failure that only surfaces once the work finishes.

### Fixed

- Turning a fan-out **off** on the canvas now persists. The component deletes `is_waiting`
  rather than writing it false, and keeps the other three keys so a toggle pressed by
  mistake costs nobody their join action — so absence of the flag has to be read as *off*
  once any of its siblings is present, rather than as a document with nothing to say.
  A state whose settings were all empty leaves nothing behind to read either way, which
  is why the editor is separately asked to write the flag out explicitly.
- The three settings survive the toggle in the payload as well as in the database. Kept
  in a column but not sent back is the same as lost, one reload later.

### Changed

- The bundled `vinta-state-machine-editor` moves from 0.8.0 to **0.9.0**: edges that share a
  source and an action are drawn as one decision card with ordered, reorderable rows rather
  than as unrelated cards, a waiting state grows the band above its hook lanes, a child state
  carries one `COUNTS AS` line instead of two chips, and cards carry advisory stripes for the
  problems the backend refuses at publish. Ordering is positional as it already was, so
  dragging a decision row is reordering the `transitions` array and nothing about the document
  schema moved.
- `editor-strings.js` gains the `decision`, `waiting` and `issue` groups, so the new surfaces
  are translated with the rest rather than being an island of English.

## [0.5.0] - 2026-09-02

### Added

- **The canvas speaks the admin's language.** Every word the editor and its dialogs put in
  front of a person — and the notes the glue writes under the save button — is marked in
  `static/vinta_state_machines/editor-strings.js` and assigned to the component's `strings`
  property, so a canvas embedded in a translated admin is no longer an island of English.
  A project with no translations reads exactly what it read before: the msgids *are* the
  component's own English.
- `editor/i18n.js` on both canvas admins, Django's `JavaScriptCatalog` for the `djangojs`
  domain, loaded ahead of the glue by `_canvas_head.html`. Serving a catalog rather than
  rendering the strings from Python is what gets plurals right: the counts on a canvas —
  side effects on a chip, items in a parameter list — are the browser's, and only the
  catalog carries the language's own `Plural-Forms` rule to pick a form with. Which app
  catalogs it carries is `editor_i18n_packages` on the ModelAdmin; `LOCALE_PATHS` is always
  merged in, so a project translates the canvas where it translates everything else.
- The four strings in the component's `seed` group are translated with the rest, which
  translates the graph rather than the chrome: they are the names a newly drawn state or
  transition is born with, and a new state's vocabulary key is slugified from its name.
  Nothing structural rides on them — a creation edge is one with no source, whatever it is
  called.

### Changed

- The bundled `vinta-state-machine-editor` moves from 0.7.0 to **0.8.0**, which puts every word
  the canvas and its dialogs say behind an overridable `strings` property — a partial set
  replaces only what it names, and the strings that take values are functions rather than
  templates, so plurals and word order stay the sentence's own business. The document schema is
  unchanged, so nothing that reads or writes a canvas document had to move. What the admin does
  with that property is the entry above.

## [0.4.0] - 2026-08-31

### Added

- **Editing a machine publishes a new version.** A `StateMachine`'s change form now carries
  the canvas too, opened on the machine's latest version whatever its lifecycle. *Save and
  publish a new version* writes a fresh version, validates it, publishes it and makes it the
  default, in one transaction — nothing already published is touched, so records that pinned
  the old graph go on validating against it. Where 0.3.0 put the canvas on the two **add**
  forms, this is the same move for a machine that already exists: versioning as a
  consequence of editing rather than one more thing to remember.

  The document remembers which version it was serialized from, so a canvas left open in one
  tab while another published is refused rather than landing on top of work it never saw. A
  graph that would not pass `validate_version` is refused whole, with every reason at once
  and no draft left behind.
- `publish_editor_machine(machine, document)` behind it, returning the published version and
  the warnings that did not block it. The new version is built from the document rather than
  cloned from the one it was drawn on — the two describe the same graph, and the rows would
  collide on a transition's name. `any_transition` hooks cannot be drawn, so they are carried
  across each revision explicitly.
- **Clone selected versions as new drafts**, an admin action on the version changelist, for
  the finer-grained path: it deep copies a version's states, transitions, hooks and layout
  into a new draft and leaves the version it copied alone.
- `next_version_label(machine)`, which both of the above take their label from: it bumps the
  trailing number — `"1"` to `"2"`, `"2024.1"` to `"2024.2"`, `"v3"` to `"v4"` — and skips
  anything already taken.
- `StateMachine.latest_version()`, the most recently created version whatever its lifecycle.
  Deliberately not `editor_machine_template`'s "newest version with anything on it": that one
  seeds a canvas, while this has to agree with the label the next version gets and with the
  stamp that catches a stale canvas.

### Changed

- The canvas partial takes its save-button label, its read-only note and whether landing a
  save reloads the page from the admin rendering it, so the two forms can say what their own
  save actually does.
- The bundled `vinta-state-machine-editor` moves from 0.5.0 to **0.7.0**. 0.6.0 replaced the
  browser's own `confirm()` with a dialog of the component's making, and 0.7.0 added theming
  and overridable icons. The document schema is unchanged across both, so nothing that reads
  or writes a canvas document had to move.
- The canvas follows the admin's colour scheme, on Django's own admin and on
  [Unfold](https://unfoldadmin.com/). The component defaults to dark and deliberately never
  reads `prefers-color-scheme` — an embedded canvas should look like the page around it, not
  like the machine it runs on — so the glue forwards whichever scheme the surrounding admin
  settled on. The two announce it differently, both on `<html>`: Unfold writes the resolved
  scheme to the class list, having already settled `auto` against the media query itself,
  while Django writes the unresolved choice to `data-theme` and leaves `auto` to its
  stylesheet. Without this the upgrade would have framed a dark canvas in a light admin.
- The canvas no longer takes any of its palette from the admin's CSS variables. It used to
  set `--sme-accent`, `--sme-surface` and `--sme-canvas` from `--primary`, `--body-bg` and
  `--darkened-bg`, which was wrong twice over: the component defines each of those tokens
  once per theme, and a custom property set from outside a shadow tree beats the `:host`
  rule defining it, so three admin colours punched holes in whichever theme was in force;
  and on an admin that defines no such variables — Unfold defines none of them — they
  collapsed to Django's light-mode values hard-coded, on a page that may well be dark. The
  scheme is forwarded instead and the component colours itself. What chrome remains outside
  the canvas keeps its `var()` on the admin's variable but falls back to a value that reads
  on a light and a dark page alike.

  Nothing had to be done to isolate the component itself: every element it registers
  attaches an open shadow root and styles itself inside it, so no admin stylesheet — Unfold's
  Tailwind included — reaches the canvas.

## [0.3.0] - 2026-08-30

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

[Unreleased]: https://github.com/vintasoftware/vinta-django-state-machines/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/vintasoftware/vinta-django-state-machines/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/vintasoftware/vinta-django-state-machines/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/vintasoftware/vinta-django-state-machines/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/vintasoftware/vinta-django-state-machines/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/vintasoftware/vinta-django-state-machines/releases/tag/v0.1.0
