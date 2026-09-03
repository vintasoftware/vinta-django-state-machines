// Glue between the admin change form and <state-machine-editor>.
//
// It also forwards the admin's colour scheme to the canvas: the component defaults
// to dark and never reads `prefers-color-scheme` itself, so without this a light
// admin would frame a dark editor. Django's admin and Unfold say which scheme is in
// force in different ways; see the theme section below.
//
// Everything the component needs from the server is injected rather than fetched by
// the component itself: the machine document, the side-effect and action catalogs and
// the guard validator all come from endpoints on this ModelAdmin, whose URLs are on
// the container's data attributes.  Its labels come from one more of them: the
// ``djangojs`` catalog, loaded ahead of this module, which `editor-strings.js` reads
// so the canvas and the notes below speak the admin's language rather than English.
//
// Two modes, told apart by which of those attributes are set:
//
// * `machine-url` — a saved version. The graph is loaded from that endpoint and
//   posted back to it by the save button, independently of the form. A save that
//   publishes a new version leaves the rest of the page stale, so `reload-on-save`
//   asks for a reload once it lands.
// * `field` — an add form, for a row that does not exist yet. The document rides
//   along in the hidden field of that name and the form's own Save applies it, so
//   there is nothing of ours to press. `template-url` plus `source-field` seed the
//   canvas from the machine picked in that select — a new version starts from the
//   previous one rather than from an empty canvas.
import './state-machine-editor.js';
import { editorStrings, fill, gettext } from './editor-strings.js';

const container = document.getElementById('dsm-editor');
if (container) {
  const editor = container.querySelector('state-machine-editor');
  const saveButton = container.querySelector('[data-dsm-save]');
  const status = container.querySelector('[data-dsm-status]');
  const readOnly = container.dataset.readonly === '1';
  const reloadOnSave = container.dataset.reloadOnSave === '1';
  const field = container.dataset.field
    ? document.querySelector(`[name="${container.dataset.field}"]`)
    : null;

  const url = (name) => container.dataset[name];
  const csrf = () =>
    (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]*)/) || [])[1] || '';

  const say = (message, kind) => {
    status.textContent = message;
    status.className = kind ? `dsm-editor__status dsm-editor__status--${kind}` : 'dsm-editor__status';
  };

  const getJson = async (target) => {
    const response = await fetch(target, { headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    return response.json();
  };

  const postJson = async (target, body) => {
    const response = await fetch(target, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        'X-CSRFToken': csrf(),
      },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error((payload.errors || []).join(' ') || response.statusText);
    return payload;
  };

  editor.readOnly = readOnly;
  // Before anything is drawn: `seed` names the elements a person adds, and those
  // names are saved into the version rather than drawn over it.
  editor.strings = editorStrings();
  editor.sideEffectProvider = () => getJson(url('sideEffectsUrl'));
  editor.actionProvider = () => getJson(url('actionsUrl'));
  editor.guardValidator = (expression) => postJson(url('guardUrl'), { expression });

  // -- theme: follow the admin's scheme --------------------------------------
  //
  // The component defaults to dark and deliberately ignores `prefers-color-scheme`,
  // on the grounds that an embedded canvas should look like the page around it
  // rather than like the machine it runs on. That makes the surrounding admin's
  // choice the one to forward — and the two admins worth supporting announce it
  // differently, both on <html>:
  //
  // * Unfold, and other Tailwind based themes, put the *resolved* scheme on the
  //   class list. It settles `auto` against the media query itself before writing
  //   one, so a class, when present, is already the answer.
  // * Django's own toggle writes the *unresolved* choice to `data-theme`, leaving
  //   `auto` to its stylesheet's media query — so `auto` falls through here too.
  const media = window.matchMedia
    ? window.matchMedia('(prefers-color-scheme: dark)')
    : null;

  const adminTheme = () => {
    const root = document.documentElement;
    if (root.classList.contains('dark')) return 'dark';
    if (root.classList.contains('light')) return 'light';
    const chosen = root.dataset.theme;
    if (chosen === 'light' || chosen === 'dark') return chosen;
    // `auto`, or an admin with no toggle at all.
    return media && media.matches ? 'dark' : 'light';
  };

  const syncTheme = () => {
    editor.theme = adminTheme();
  };

  syncTheme();
  // Both admins rewrite the attribute in place; neither dispatches anything to
  // listen for, so the element has to be watched.
  new MutationObserver(syncTheme).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme', 'class'],
  });
  // Only bites while the admin is on `auto` and said so by writing nothing, but it
  // costs nothing to keep attached: `adminTheme` re-reads the element first and
  // never reaches the query when the admin has settled the question itself.
  if (media) media.addEventListener('change', syncTheme);

  let dirty = false;

  // -- form mode: the document is a field of the form ------------------------

  const seedElement = document.getElementById('dsm-editor-seed');
  const seed = seedElement ? JSON.parse(seedElement.textContent) : null;

  const parseField = () => {
    if (!field || !field.value.trim()) return null;
    try {
      return JSON.parse(field.value);
    } catch {
      return null;
    }
  };

  const mirror = () => {
    if (field) field.value = JSON.stringify(editor.value);
  };

  const show = (machine) => {
    editor.value = machine;
    editor.zoomToFit();
    mirror();
  };

  /** The machine whose latest version a new one is drawn from, if one is picked. */
  const sourceSelect = container.dataset.sourceField
    ? document.getElementById(container.dataset.sourceField)
    : null;

  // Which machine the canvas is currently showing, so a pick announced twice —
  // see below — is fetched once.
  let showing = null;

  const loadTemplate = async () => {
    const chosen = sourceSelect ? sourceSelect.value : '';
    if (!url('templateUrl') || chosen === showing) return;
    showing = chosen;
    try {
      const target = `${url('templateUrl')}?state_machine=${encodeURIComponent(chosen)}`;
      show(await getJson(target));
      say(
        chosen
          ? gettext('Starting from the latest version of this machine.')
          : gettext('Draw the graph here; it is saved with the rest of the form.'),
      );
    } catch (error) {
      showing = null; // Let the next pick — or the same one again — retry.
      say(
        fill(gettext('Could not load the previous version: %(reason)s'), {
          reason: error.message,
        }),
        'error',
      );
    }
  };

  if (field) {
    // What the form came back with wins over any template: a redisplayed form is
    // one somebody's graph was refused on, and re-seeding would throw it away.
    const posted = parseField();
    if (posted) {
      show(posted);
    } else if (sourceSelect && sourceSelect.value) {
      loadTemplate();
    } else if (seed) {
      show(seed);
    }
    if (sourceSelect) {
      sourceSelect.addEventListener('change', loadTemplate);
      // The admin renders a foreign key listed in `autocomplete_fields` as a
      // select2 widget, which announces a pick by triggering jQuery's own change
      // rather than dispatching a DOM event anything else can hear.
      const jquery = window.django && window.django.jQuery;
      if (jquery) jquery(sourceSelect).on('change', loadTemplate);
    }
    // Belt and braces: every change already mirrors, but a gesture still in flight
    // when the form is submitted has not fired its committed change yet.
    const form = field.closest('form');
    if (form) form.addEventListener('submit', mirror);
  }

  // -- live mode: the graph has an endpoint of its own -----------------------

  if (url('machineUrl')) {
    getJson(url('machineUrl'))
      .then((machine) => {
        editor.value = machine;
        editor.zoomToFit();
        // Keep what the page came with — the read-only note — and what the
        // assignment itself had to say: a graph stored without coordinates is laid
        // out on the way in, and that layout is real work waiting to be saved.
        if (!readOnly && !dirty) say('');
      })
      .catch((error) =>
        say(
          fill(gettext('Could not load the graph: %(reason)s'), { reason: error.message }),
          'error',
        ),
      );
  }

  // A fan-out crosses into another machine, and the canvas only draws one. The
  // component says where the user wants to go and stops; this is the part that takes
  // them there. Search rather than a direct link: a machine key is not a primary key,
  // and the changelist already searches on it.
  editor.addEventListener('state-machine-fan-out', (event) => {
    const key = event.detail && event.detail.childMachine;
    const machines = url('machinesUrl');
    if (!key || !machines) {
      say(gettext('This state does not name the machine its children belong to.'), 'error');
      return;
    }
    if (dirty && !window.confirm(gettext('Leave this graph? Your changes are not saved.'))) {
      return;
    }
    window.location.href = `${machines}?q=${encodeURIComponent(key)}`;
  });

  editor.addEventListener('state-machine-change', (event) => {
    // Mid-drag frames are not worth marking the form dirty over.
    if (event.detail.transient) return;
    mirror();
    // A read-only canvas still lays an unpositioned graph out, and there is nothing
    // to do about that here: no button to save it with and nothing to warn about.
    if (readOnly) return;
    dirty = true;
    if (!field) say(gettext('Unsaved changes.'), 'dirty');
  });

  if (saveButton) {
    saveButton.addEventListener('click', async () => {
      saveButton.disabled = true;
      say(gettext('Saving…'));
      try {
        // The server is the authority on ids: a state drawn here arrives with a
        // generated one and comes back keyed by the vocabulary key it was given.
        const saved = await postJson(url('machineUrl'), editor.value);
        dirty = false;
        if (reloadOnSave) {
          // What was published shows up in the rest of the page, and the messages
          // the server queued for it are waiting on the next request.
          say(gettext('Saved. Reloading…'), 'ok');
          window.location.reload();
          return;
        }
        editor.value = saved;
        say(gettext('Saved.'), 'ok');
      } catch (error) {
        say(error.message, 'error');
      } finally {
        saveButton.disabled = false;
      }
    });
  }

  window.addEventListener('beforeunload', (event) => {
    // In form mode the graph goes with the form, so its own Save is what settles it.
    if (!dirty || field) return;
    event.preventDefault();
    event.returnValue = '';
  });
}
