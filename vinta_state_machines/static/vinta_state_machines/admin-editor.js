// Glue between the admin change form and <state-machine-editor>.
//
// Everything the component needs from the server is injected rather than fetched by
// the component itself: the machine document, the side-effect and action catalogs and
// the guard validator all come from endpoints on this ModelAdmin, whose URLs are on
// the container's data attributes.
import './state-machine-editor.js';

const container = document.getElementById('dsm-editor');
if (container) {
  const editor = container.querySelector('state-machine-editor');
  const saveButton = container.querySelector('[data-dsm-save]');
  const status = container.querySelector('[data-dsm-status]');
  const readOnly = container.dataset.readonly === '1';

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
  editor.sideEffectProvider = () => getJson(url('sideEffectsUrl'));
  editor.actionProvider = () => getJson(url('actionsUrl'));
  editor.guardValidator = (expression) => postJson(url('guardUrl'), { expression });

  let dirty = false;

  getJson(url('machineUrl'))
    .then((machine) => {
      editor.value = machine;
      editor.zoomToFit();
      say('');
    })
    .catch((error) => say(`Could not load the graph: ${error.message}`, 'error'));

  editor.addEventListener('state-machine-change', (event) => {
    // Mid-drag frames are not worth marking the form dirty over.
    if (event.detail.transient) return;
    dirty = true;
    say('Unsaved changes.', 'dirty');
  });

  if (saveButton) {
    saveButton.addEventListener('click', async () => {
      saveButton.disabled = true;
      say('Saving…');
      try {
        // The server is the authority on ids: a state drawn here arrives with a
        // generated one and comes back keyed by the vocabulary key it was given.
        const saved = await postJson(url('machineUrl'), editor.value);
        editor.value = saved;
        dirty = false;
        say('Saved.', 'ok');
      } catch (error) {
        say(error.message, 'error');
      } finally {
        saveButton.disabled = false;
      }
    });
  }

  window.addEventListener('beforeunload', (event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = '';
  });
}
