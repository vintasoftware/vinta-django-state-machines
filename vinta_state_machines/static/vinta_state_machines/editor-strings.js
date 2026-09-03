// Every word `<state-machine-editor>` says, in the admin's active language.
//
// The component ships English and takes no view on locales: a host assigns its own
// set through the `strings` property, from whatever translation machinery it already
// runs.  In Django that machinery is `JavaScriptCatalog`, which the editor mixin
// serves beside its other endpoints and `_canvas_head.html` loads before this module.
// It defines `django.gettext`, `django.ngettext` and `django.interpolate` — and, from
// the catalog's own `Plural-Forms`, the `pluralidx` that picks between plural forms.
// That last part is why the strings are marked here rather than rendered as JSON from
// Python: how many forms a count needs, and which one it gets, is a question only the
// browser can answer, because only the browser knows the count.
//
// The idiom is Django's own, `interpolate(gettext(…), …, true)` spelled `fill`: the
// `gettext` and `ngettext` calls stay where `xgettext` can see them, so the msgids
// come out of `makemessages -d djangojs` without a keyword of our own.
//
// Every msgid is the component's own English, so a project with no catalog for its
// language reads exactly what it read before any of this existed.  Entries that are
// pure punctuation — `[guard]`, `{ } 3`, `⚡ pay` — are left out rather than marked:
// there is nothing in them to translate, and a partial set keeps the rest as it was.
//
// A key this file names that the component does not have is ignored silently, so the
// group names and keys below have to match `EditorStrings` exactly; the test suite
// checks each of them against the vendored bundle.

/** The catalog, or a stand-in that keeps the editor in English if it never loaded. */
const catalog = () => {
  const loaded = window.django;
  return {
    gettext: loaded ? loaded.gettext : (msgid) => msgid,
    ngettext: loaded
      ? loaded.ngettext
      : (singular, pluralMsgid, count) => (count === 1 ? singular : pluralMsgid),
    interpolate: loaded
      ? loaded.interpolate
      : (fmt, obj) => fmt.replace(/%\(\w+\)s/g, (match) => String(obj[match.slice(2, -2)])),
  };
};

// Bound late, so the catalog script is only read once it has run.

/** One message in the active language. */
export const gettext = (msgid) => catalog().gettext(msgid);

/** The form of a message that `count` calls for, by the catalog's own plural rule. */
const ngettext = (singular, pluralMsgid, count) =>
  catalog().ngettext(singular, pluralMsgid, count);

/** A translated format string with its named parameters filled in. */
export const fill = (fmt, params) => catalog().interpolate(fmt, params, true);

/**
 * What to assign to the component's `strings`.
 *
 * Read at setup rather than at import: `seed` names the elements a person draws, so
 * it has to be in place before the canvas is touched, and the toolbar and the dialogs
 * take theirs from here as they are built.
 */
export const editorStrings = () => ({
  toolbar: {
    label: gettext('Editor tools'),
    addState: gettext('Add state'),
    undo: gettext('Undo'),
    redo: gettext('Redo'),
    undoChange: ({ change }) => fill(gettext('Undo %(change)s'), { change }),
    redoChange: ({ change }) => fill(gettext('Redo %(change)s'), { change }),
    copy: gettext('Copy'),
    copyKind: ({ kind }) => fill(gettext('Copy %(kind)s'), { kind }),
    paste: gettext('Paste'),
    pasteKind: ({ kind }) => fill(gettext('Paste %(kind)s'), { kind }),
    organize: gettext('Organize'),
    organizeLabel: gettext('Organize layout'),
    zoomOut: gettext('Zoom out'),
    zoomIn: gettext('Zoom in'),
    zoomReset: gettext('Reset zoom to 100%'),
    fit: gettext('Fit'),
    fitLabel: gettext('Zoom to fit'),
    themeLight: gettext('Switch to the light theme'),
    themeDark: gettext('Switch to the dark theme'),
  },
  canvas: {
    empty: gettext('No states yet — use “Add state” to start.'),
  },
  kind: {
    state: gettext('state'),
    transition: gettext('transition'),
  },
  card: {
    toolsLabel: ({ name }) => fill(gettext('Tools for “%(name)s”'), { name }),
  },
  state: {
    rename: gettext('Rename state'),
    properties: gettext('State properties'),
    remove: gettext('Remove state'),
    link: gettext('Drag to another state to create a transition'),
    nameLabel: gettext('State name'),
    colorLabel: ({ color }) => fill(gettext('Colour: %(color)s. Pick another.'), { color }),
    colorTitle: ({ color }) => fill(gettext('Colour: %(color)s'), { color }),
    paletteLabel: ({ name }) => fill(gettext('Colour of “%(name)s”'), { name }),
    roleInitial: gettext('Initial'),
    roleFinal: gettext('Final'),
    markInitial: ({ name }) => fill(gettext('Mark “%(name)s” as an initial state'), { name }),
    unmarkInitial: ({ name }) => fill(gettext('Unmark “%(name)s” as an initial state'), { name }),
    markFinal: ({ name }) => fill(gettext('Mark “%(name)s” as a final state'), { name }),
    unmarkFinal: ({ name }) => fill(gettext('Unmark “%(name)s” as a final state'), { name }),
    creationAdd: gettext('Creation'),
    creationTitle: gettext('Add a transition that creates a record in this state'),
    creationLabel: ({ name }) =>
      fill(gettext('Add a creation transition into “%(name)s”'), { name }),
  },
  color: {
    neutral: gettext('neutral'),
    info: gettext('info'),
    success: gettext('success'),
    warning: gettext('warning'),
    danger: gettext('danger'),
    muted: gettext('muted'),
  },
  rename: {
    title: gettext('Rename (F2)'),
    save: gettext('Save name'),
    saveTitle: gettext('Save (Enter)'),
    cancel: gettext('Cancel renaming'),
    cancelTitle: gettext('Cancel (Escape)'),
  },
  transition: {
    rename: gettext('Rename transition'),
    properties: gettext('Transition properties'),
    remove: gettext('Remove transition'),
    nameLabel: gettext('Transition name'),
    triggerTitle: ({ name }) => fill(gettext('Trigger: %(name)s'), { name }),
    guardTitle: ({ guard }) => fill(gettext('Guard: %(guard)s'), { guard }),
  },
  startNode: {
    label: gettext('Create'),
    title: gettext('Every transition leaving here creates a record'),
    link: gettext('Drag to a state to create a creation transition'),
    summary: ({ label, count }) =>
      fill(
        ngettext(
          '%(label)s: %(count)s creation transition',
          '%(label)s: %(count)s creation transitions',
          count,
        ),
        { label, count },
      ),
  },
  source: {
    start: gettext('the start'),
    // Where a name's quotation marks live, and they are not the same everywhere.
    state: ({ name }) => fill(gettext('“%(name)s”'), { name }),
  },
  chip: {
    add: gettext('Add side effect'),
    empty: gettext('No side effects'),
    // Two whole sentences rather than one glued together, so the clause about
    // parameters can sit where the language wants it.
    label: ({ description, count, withParams }) =>
      withParams > 0
        ? fill(
            ngettext(
              '%(description)s %(count)s side effect, %(withParams)s with parameters. Open list.',
              '%(description)s %(count)s side effects, %(withParams)s with parameters. Open list.',
              count,
            ),
            { description, count, withParams },
          )
        : fill(
            ngettext(
              '%(description)s %(count)s side effect. Open list.',
              '%(description)s %(count)s side effects. Open list.',
              count,
            ),
            { description, count },
          ),
  },
  phase: {
    before: gettext('before'),
    after: gettext('after'),
  },
  trigger: {
    enter: gettext('enter'),
    leave: gettext('leave'),
  },
  triggerVerb: {
    enter: gettext('entering'),
    leave: gettext('leaving'),
  },
  sideEffect: {
    disabled: ({ name }) => fill(gettext('%(name)s (off)'), { name }),
    summary: ({ head, count }) =>
      fill(ngettext('%(head)s and %(count)s more', '%(head)s and %(count)s more', count), {
        head,
        count,
      }),
    titleEntry: ({ index, name, params, disabled }) =>
      disabled
        ? fill(gettext('%(index)s. %(name)s%(params)s — disabled'), { index, name, params })
        : fill(gettext('%(index)s. %(name)s%(params)s'), { index, name, params }),
  },
  sideEffects: {
    stateTitle: ({ phase, verb }) =>
      fill(gettext('Side effects · %(phase)s %(verb)s'), { phase, verb }),
    stateDescription: ({ phase, verb, name }) =>
      fill(gettext('Runs %(phase)s %(verb)s the state “%(name)s”.'), { phase, verb, name }),
    transitionTitle: ({ phase }) =>
      fill(gettext('Side effects · %(phase)s transition'), { phase }),
    transitionDescription: ({ phase, name }) =>
      fill(gettext('Runs %(phase)s the transition “%(name)s”.'), { phase, name }),
    listLabel: gettext('Side effects, in execution order'),
    empty: gettext('No side effects yet.'),
    selectLabel: gettext('Side effect to add'),
    add: gettext('Add'),
    placeholder: gettext('Select a side effect…'),
    noCatalog: gettext('No side effect catalog was provided.'),
    loading: gettext('Loading side effects…'),
    invalidCatalog: ({ errors }) =>
      fill(gettext('Invalid side effect catalog: %(errors)s'), { errors }),
    loadFailed: ({ reason }) =>
      fill(gettext('Could not load side effects: %(reason)s'), { reason }),
    pickOne: gettext('Pick a side effect to add.'),
    catalogOption: ({ name, description }) =>
      fill(gettext('%(name)s — %(description)s'), { name, description }),
  },
  row: {
    reorderLabel: ({ name, index, total }) =>
      fill(
        gettext(
          'Reorder %(name)s. Position %(index)s of %(total)s. Use Alt with arrow keys to move.',
        ),
        { name, index, total },
      ),
    reorderTitle: gettext('Drag to reorder, or press Alt + Arrow Up/Down'),
    enabledLabel: ({ name }) => fill(gettext('Run %(name)s'), { name }),
    enabledTitle: gettext('Run this side effect'),
    // The branch is the sentence's, not the component's: a language may want more
    // than one verb swapped between the two.
    paramsLabel: ({ name, count, expanded }) =>
      expanded
        ? fill(gettext('Hide parameters of %(name)s, %(count)s set'), { name, count })
        : fill(gettext('Edit parameters of %(name)s, %(count)s set'), { name, count }),
    paramsTitle: gettext('JSON parameters'),
    remove: ({ name }) => fill(gettext('Remove %(name)s'), { name }),
    descriptionLabel: ({ name }) => fill(gettext('Description of %(name)s'), { name }),
    descriptionPlaceholder: gettext('Description'),
  },
  params: {
    editorLabel: ({ name }) => fill(gettext('Parameter editor for %(name)s'), { name }),
    jsonLabel: ({ name }) => fill(gettext('Parameters of %(name)s as JSON'), { name }),
    modeForm: gettext('Form'),
    modeJson: gettext('JSON'),
  },
  // A decision card: several edges leaving one state under one action, which the
  // engine resolves by trying each in turn. `else` and `unreachable` are left as the
  // component's own words on purpose -- they are the vocabulary of the thing, and a
  // translation that renames them stops matching what the guards read like.
  decision: {
    outcomes: ({ count }) =>
      fill(ngettext('%(count)s outcome', '%(count)s outcomes', count), { count }, true),
    label: ({ action, count }) =>
      fill(
        ngettext(
          '%(action)s: %(count)s outcome, tried in order.',
          '%(action)s: %(count)s outcomes, tried in order.',
          count,
        ),
        { action, count },
        true,
      ),
    fallbackTitle: gettext('No guard — runs when none of the rows above matched'),
    orderTitle: ({ index, total }) =>
      fill(gettext('Tried %(index)s of %(total)s'), { index, total }),
    dead: gettext('unreachable'),
    deadTitle: gettext(
      'Never reached: a row above has no guard, so it always matches first',
    ),
    targetTitle: ({ name }) => fill(gettext('Goes to %(name)s'), { name }),
    rowLabel: ({ outcome, target, index, total, expanded }) =>
      fill(
        expanded
          ? gettext('Hide outcome %(index)s of %(total)s: %(outcome)s %(target)s')
          : gettext('Edit outcome %(index)s of %(total)s: %(outcome)s %(target)s'),
        { outcome, target, index, total },
      ),
    reorderLabel: ({ outcome, index, total }) =>
      fill(
        gettext(
          'Reorder %(outcome)s. Position %(index)s of %(total)s. ' +
            'Use Alt with arrow keys to move.',
        ),
        { outcome, index, total },
      ),
    reorderTitle: gettext('Drag to reorder, or press Alt + Arrow Up/Down'),
    fieldsLabel: ({ name }) => fill(gettext('Fields of “%(name)s”'), { name }),
    fieldName: gettext('Name'),
  },

  // The band on a state that fans work out, and the one line a child state carries
  // to say it counts towards its parent's batch.
  waiting: {
    role: gettext('Waiting'),
    mark: ({ name }) =>
      fill(gettext('Mark “%(name)s” as a state that waits for a batch'), { name }),
    unmark: ({ name }) =>
      fill(gettext('Unmark “%(name)s” as a state that waits for a batch'), { name }),
    bandLabel: ({ name }) => fill(gettext('The batch “%(name)s” waits for'), { name }),
    fansOut: gettext('Fans out to'),
    fansOutLink: ({ machine, name }) =>
      fill(gettext('Open the machine “%(machine)s” that “%(name)s” fans out to'), {
        machine,
        name,
      }),
    fansOutTitle: ({ machine }) => fill(gettext('Open %(machine)s'), { machine }),
    stubLabel: ({ name, machine }) =>
      fill(gettext('“%(name)s” starts records governed by %(machine)s'), { name, machine }),
    joinsWith: gettext('Joins with'),
    timeout: gettext('Timeout'),
    countsAs: gettext('Counts as'),
    outcome: {
      success: gettext('✓ success'),
      failure: gettext('✗ failure'),
    },
    pairTitle: gettext('Reported on entering, and taken back on leaving'),
    enterOnly: ({ outcome }) => fill(gettext('%(outcome)s · on enter only'), { outcome }),
    enterOnlyTitle: gettext(
      'A final state can never be left, so nothing takes the report back — ' +
        'the leave half is dropped.',
    ),
    broken: ({ outcome }) => fill(gettext('%(outcome)s · half configured'), { outcome }),
    brokenError: ({ half }) =>
      fill(
        gettext(
          'Only the %(half)s half of this report is here. ' +
            'The pair has to be whole on a state that can be left.',
        ),
        { half },
      ),
    half: {
      enter: gettext('enter'),
      leave: gettext('leave'),
    },
    unset: gettext('not set'),
    rowLabel: ({ field, value, name }) =>
      fill(gettext('%(field)s: %(value)s. Edit the attributes of “%(name)s”.'), {
        field,
        value,
        name,
      }),
    section: gettext('Waiting for a batch'),
  },

  // The advisory stripes. They say where a problem is while the person who caused it
  // is still looking at the card; the backend is what actually refuses to publish.
  issue: {
    label: gettext('Problems with this card'),
    noFallback: gettext('No fallback — if every guard fails the record is stuck here.'),
    noJoinEdge: gettext('Nothing leaves this state when the work finishes.'),
    zeroTimeout: gettext('A timeout of zero leaves the batch no time to finish.'),
    terminalHasExit: gettext(
      'Terminal states cannot be left, so these edges never fire.',
    ),
  },

  properties: {
    title: ({ name }) => fill(gettext('Properties · %(name)s'), { name }),
    stateDescription: ({ name }) => fill(gettext('Attributes of the state “%(name)s”.'), { name }),
    transitionDescription: ({ source, target }) =>
      fill(gettext('Attributes of the transition from %(source)s to %(target)s.'), {
        source,
        target,
      }),
    fieldTrigger: gettext('Trigger'),
    triggerHint: gettext('No action catalog was provided, so the trigger is free text.'),
    triggerPlaceholder: gettext('e.g. pay'),
    triggerNone: gettext('No trigger'),
    actionsLoading: gettext('Loading actions…'),
    actionsInvalid: ({ errors }) =>
      fill(gettext('Invalid action catalog: %(errors)s'), { errors }),
    actionsLoadFailed: ({ reason }) =>
      fill(gettext('Could not load actions: %(reason)s'), { reason }),
    fieldGuard: gettext('Guard'),
    guardLabel: gettext('Guard expression'),
    guardPlaceholder: gettext('Condition the host evaluates'),
    fieldPermission: gettext('Required permission'),
    permissionPlaceholder: gettext('e.g. orders.pay'),
    fieldDescription: gettext('Description'),
    fieldOrder: gettext('Order'),
    orderHint: ({ source }) =>
      fill(gettext('Edges leaving %(source)s are evaluated in this order.'), { source }),
    orderReadout: ({ index, total }) => fill(gettext('%(index)s of %(total)s'), { index, total }),
    moveUp: gettext('Move earlier'),
    moveDown: gettext('Move later'),
  },
  // What an undo or redo would take back, read into the toolbar's own sentence.
  change: {
    'state-add': gettext('add state'),
    'state-remove': gettext('remove state'),
    'state-rename': gettext('rename state'),
    'state-move': gettext('move state'),
    'state-color': gettext('change state colour'),
    'transition-add': gettext('add transition'),
    'transition-remove': gettext('remove transition'),
    'transition-rename': gettext('rename transition'),
    'transition-move': gettext('move transition'),
    'transition-trigger': gettext('change transition trigger'),
    'transition-guard': gettext('change transition guard'),
    'transition-permission': gettext('change required permission'),
    'transition-reorder': gettext('reorder transitions'),
    description: gettext('change description'),
    'side-effects-change': gettext('change side effects'),
    layout: gettext('organize layout'),
    'initial-states-change': gettext('change initial states'),
    'final-states-change': gettext('change final states'),
    replace: gettext('replace machine'),
  },
  dialog: {
    save: gettext('Save'),
    cancel: gettext('Cancel'),
    close: gettext('Close'),
    confirm: gettext('Confirm'),
  },
  organize: {
    title: gettext('Organize the layout?'),
    // One literal on one line on purpose: `xgettext` will not fold a message split
    // across a concatenation, and a msgid it cannot see is a msgid nobody translates.
    message: gettext('Every card is moved onto the automatic layout. The positions on the canvas now — including the ones you dragged — are lost, though a single undo brings them back.'),
    confirm: gettext('Organize'),
  },
  json: {
    empty: gettext('No parameters.'),
    addItem: gettext('Add item'),
    addField: gettext('Add field'),
    keyLabel: ({ label }) => fill(gettext('Name of parameter %(label)s'), { label }),
    typeLabel: ({ label }) => fill(gettext('Type of %(label)s'), { label }),
    valueLabel: ({ label }) => fill(gettext('Value of %(label)s'), { label }),
    removeLabel: ({ label }) => fill(gettext('Remove %(label)s'), { label }),
    itemCount: ({ count }) =>
      fill(ngettext('%(count)s item', '%(count)s items', count), { count }),
    fieldCount: ({ count }) =>
      fill(ngettext('%(count)s field', '%(count)s fields', count), { count }),
    nullValue: gettext('null'),
    invalid: gettext('Invalid JSON.'),
    notJsonValues: gettext('Parameters must contain only JSON values.'),
    notObject: gettext('Parameters must be a JSON object, for example {"to": "user"}.'),
  },
  // Not labels drawn over the data: these are the names a new element is born with,
  // and they are saved into the version.  A new state's vocabulary key is slugified
  // from its name, so translating these translates that key too — which is what a
  // person drawing a graph in their own language is asking for.  Nothing here is
  // structural: a creation edge is one with no source, whatever it is called.
  seed: {
    stateName: ({ index }) => fill(gettext('State %(index)s'), { index }),
    transitionName: gettext('transition'),
    creationName: gettext('create'),
    copySuffix: gettext('copy'),
  },
});
