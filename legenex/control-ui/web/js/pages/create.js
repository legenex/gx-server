// CREATE: image and video generation and editing (D-034).
// Every option shown here is one the media router actually applies.
import { api } from '../api.js';
import { h, clear, toast, errorBox, kv, duration } from '../dom.js';
import {
  PHASE_TEXT, mediaEl, metaRows, phaseTrack, pickAsset, pollMediaJob, uploadSource,
} from './media-common.js';

const TABS = [
  { id: 'image', kind: 't2i', label: 'Generate Image', source: null },
  { id: 'edit', kind: 'edit', label: 'Edit Image', source: 'image' },
  { id: 'video', kind: 't2v', label: 'Generate Video', source: null },
  { id: 'i2v', kind: 'i2v', label: 'Image to Video', source: 'image' },
  { id: 'vedit', kind: 'v2v', label: 'Edit Video', source: 'video' },
];

const HELP = {
  t2i: 'Text to image with Qwen-Image-2512 (4-step Lightning). The uncensored adapter is on by default.',
  edit: 'Describe the change in plain language, e.g. "Change the background to a sunset beach and make the shirt black." '
    + 'The original is never modified; the result is a new library item linked to it.',
  t2v: 'Text to video with Wan 2.2 A14B and the uncensored 4-step LoRAs. Takes about a minute for 3 seconds of 640×640.',
  i2v: 'Animate a still image. The image becomes the first frame; describe the motion.',
  v2v: 'Describe the edit, e.g. "make this scene take place at night". Strong edits (≥ 0.75) re-render the clip from an edited '
    + 'first frame (same scene, new lighting/style); lower strengths keep more of the original motion.',
};

let root;
let state;
let options = { image_sizes: [], video_sizes: [] };
let abort = null;

function field(label, input, hint) {
  const id = input.id;
  return h('div', { class: 'field' }, h('label', { for: id }, label), input,
    hint ? h('p', { class: 'hint', id: `${id}-hint` }, hint) : null);
}

function select(id, values, value) {
  return h('select', { id, name: id }, values.map((v) => h('option', { value: v, selected: v === value }, v)));
}

function numberInput(id, { min, max, step, value, placeholder }) {
  return h('input', { id, name: id, type: 'number', min, max, step, value, placeholder, inputmode: 'decimal' });
}

function sourcePanel(tab) {
  const wrap = h('div', { class: 'source-panel', role: 'group', 'aria-label': `Source ${tab.source}` });
  const preview = h('div', { class: 'source-preview' });
  const render = () => {
    clear(preview);
    if (state.source && state.source.type === tab.source) {
      preview.append(mediaEl(state.source, { controls: true }),
        h('p', { class: 'small' }, `Source: ${state.source.title || state.source.prompt || state.source.id}`));
    } else {
      preview.append(h('p', { class: 'muted' }, `No source ${tab.source} chosen yet.`));
    }
  };
  const file = h('input', {
    type: 'file', id: `src-file-${tab.id}`, accept: tab.source === 'video' ? 'video/mp4,video/quicktime,video/webm' : 'image/png,image/jpeg,image/webp',
  });
  const progress = h('span', { class: 'muted small', 'aria-live': 'polite' });
  file.addEventListener('change', async () => {
    if (!file.files.length) return;
    try {
      progress.textContent = 'Uploading…';
      state.source = await uploadSource(file.files[0], tab.source);
      progress.textContent = 'Uploaded to the library.';
      render();
    } catch (err) {
      progress.textContent = '';
      toast(err.message, 'crit', 8000);
    }
  });
  wrap.append(
    h('div', { class: 'btn-row' },
      h('button', {
        type: 'button', class: 'btn', id: `pick-${tab.id}`,
        onclick: async () => { const a = await pickAsset(tab.source); if (a) { state.source = a; render(); } },
      }, `Choose from library`),
      h('label', { class: 'btn btn-ghost file-btn', for: `src-file-${tab.id}` }, `Upload ${tab.source}`, file),
      progress),
    preview);
  render();
  return wrap;
}

function formFor(tab) {
  const f = h('form', { class: 'create-form', id: `form-${tab.id}`, novalidate: true });
  const prompt = h('textarea', {
    id: `prompt-${tab.id}`, rows: 4, maxlength: 4000, required: tab.kind !== 'variation',
    placeholder: tab.source ? 'What should change?' : 'Describe what to create', 'aria-describedby': `help-${tab.id}`,
  });
  f.append(h('p', { class: 'hint', id: `help-${tab.id}` }, HELP[tab.kind]));
  if (tab.source) f.append(sourcePanel(tab));
  f.append(field(tab.source ? 'Instruction' : 'Prompt', prompt));
  const neg = h('input', { id: `neg-${tab.id}`, maxlength: 4000, placeholder: 'optional' });
  const seed = numberInput(`seed-${tab.id}`, { min: 0, step: 1, placeholder: 'random' });
  const title = h('input', { id: `title-${tab.id}`, maxlength: 200, placeholder: 'optional' });
  const uncensored = h('input', { type: 'checkbox', id: `unc-${tab.id}`, checked: tab.kind !== 'edit' });
  const adv = h('details', { class: 'advanced' }, h('summary', {}, 'Options'));
  const grid = h('div', { class: 'form-grid' });
  adv.append(grid);
  grid.append(field('Negative prompt', neg), field('Seed', seed), field('Title', title));
  if (tab.kind === 't2i') {
    grid.append(field('Size', select(`size-${tab.id}`, options.image_sizes, '1328x1328')),
      field('Images', numberInput(`n-${tab.id}`, { min: 1, max: 4, step: 1, value: 1 })),
      field('Quality', select(`quality-${tab.id}`, ['standard', 'fast', 'hd'], 'standard'),
        'standard: 4-step + uncensored adapter · fast: 4-step, no adapter · hd: 50 steps (~4 min)'));
  }
  if (tab.kind === 'edit') {
    grid.append(field('Edit strength', numberInput(`strength-${tab.id}`, { min: 0.05, max: 1, step: 0.05, value: 1 }),
      '1.0 follows the instruction fully; lower keeps more of the original.'));
  }
  if (['t2v', 'i2v', 'v2v'].includes(tab.kind)) {
    grid.append(field('Size', select(`size-${tab.id}`, options.video_sizes, '640x640')),
      field('Seconds', numberInput(`seconds-${tab.id}`, { min: 0.5, max: 10, step: 0.5, value: 3 })));
  }
  if (['t2v', 'i2v'].includes(tab.kind)) {
    grid.append(field('FPS', numberInput(`fps-${tab.id}`, { min: 8, max: 24, step: 1, value: 16 })));
  }
  if (tab.kind === 'v2v') {
    grid.append(field('Edit strength', numberInput(`strength-${tab.id}`, { min: 0.05, max: 1, step: 0.05, value: 0.85 }),
      '≥ 0.75 instruction edit (re-rendered) · 0.5–0.75 structure-preserving · lower: light restyle'));
  }
  grid.append(h('div', { class: 'field checkbox' }, uncensored,
    h('label', { for: `unc-${tab.id}` }, tab.kind === 'edit' ? 'Apply the NSFW edit adapter (not an exact 2511 match)' : 'Uncensored adapter')));
  f.append(adv);
  const submit = h('button', { type: 'submit', class: 'btn btn-primary', id: `go-${tab.id}` }, 'Generate');
  const err = h('p', { class: 'form-error', role: 'alert', hidden: true });
  f.append(err, h('div', { class: 'btn-row' }, submit));

  f.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    err.hidden = true;
    const val = (id) => (document.getElementById(id) || {}).value;
    const body = { kind: tab.kind, prompt: prompt.value.trim(), negative_prompt: neg.value.trim() || undefined,
      title: title.value.trim() || undefined, uncensored: uncensored.checked };
    if (seed.value !== '') body.seed = Number(seed.value);
    if (!body.prompt && tab.kind !== 'variation') { err.hidden = false; err.textContent = 'Enter a prompt.'; prompt.focus(); return; }
    if (tab.source) {
      if (!state.source || state.source.type !== tab.source) {
        err.hidden = false; err.textContent = `Choose or upload a source ${tab.source}.`; return;
      }
      body.source_id = state.source.id;
    }
    for (const [k, id] of [['size', `size-${tab.id}`], ['quality', `quality-${tab.id}`]]) if (val(id)) body[k] = val(id);
    for (const [k, id] of [['n', `n-${tab.id}`], ['seconds', `seconds-${tab.id}`], ['fps', `fps-${tab.id}`], ['strength', `strength-${tab.id}`]]) {
      if (val(id) !== undefined && val(id) !== '') body[k] = Number(val(id));
    }
    submit.disabled = true;
    try {
      const job = await api.post('/api/media/jobs', body);
      toast(`${job.label} queued`, 'ok');
      track(job);
    } catch (e) {
      err.hidden = false;
      err.textContent = e.message;
    } finally {
      submit.disabled = false;
    }
  });
  return f;
}

function resultActions(asset) {
  const go = (tab) => { state.source = asset; state.tab = tab; render(); };
  const row = h('div', { class: 'btn-row' },
    h('a', { class: 'btn btn-sm', href: asset.download_url, download: '' }, 'Download'),
    h('button', {
      type: 'button', class: 'btn btn-sm',
      onclick: async () => { await api.post(`/api/media/assets/${asset.id}`, { favourite: true }); toast('Saved to favourites'); },
    }, '★ Favourite'),
    h('a', { class: 'btn btn-sm btn-ghost', href: `#/library/${asset.id}` }, 'Open in Library'));
  if (asset.type === 'image') {
    row.append(
      h('button', { type: 'button', class: 'btn btn-sm', onclick: () => go('edit') }, 'Edit again'),
      h('button', {
        type: 'button', class: 'btn btn-sm',
        onclick: async () => {
          const job = await api.post('/api/media/jobs', { kind: 'variation', source_id: asset.id });
          toast('Variation queued'); track(job);
        },
      }, 'Variation'),
      h('button', { type: 'button', class: 'btn btn-sm', onclick: () => go('i2v') }, 'Make video'));
  } else {
    row.append(h('button', { type: 'button', class: 'btn btn-sm', onclick: () => go('vedit') }, 'Edit video'));
  }
  return row;
}

function track(job) {
  const panel = document.getElementById('job-panel');
  const card = h('section', { class: 'card job-card', 'aria-live': 'polite', id: `job-${job.id}` });
  panel.prepend(card);
  const draw = async (j) => {
    clear(card).append(
      h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, j.label),
        h('span', { class: `badge badge-${j.phase === 'ready' ? 'ok' : j.phase === 'failed' ? 'crit' : 'warn'}` }, PHASE_TEXT[j.phase] || j.phase)),
      phaseTrack(j),
      h('p', { class: 'muted small' }, `${j.detail || ''} · ${duration(j.elapsed_seconds)}`),
      j.prompt ? h('p', { class: 'prompt-line' }, j.prompt) : null,
      j.error ? h('p', { class: 'callout callout-danger' }, j.error) : null);
    if (j.phase === 'ready') {
      for (const id of j.assets) {
        try {
          const a = await api.get(`/api/media/assets/${id}`);
          card.append(h('div', { class: 'result' }, mediaEl(a), resultActions(a),
            h('details', {}, h('summary', {}, 'Metadata'), kv(metaRows(a)))));
        } catch (e) { card.append(errorBox(e)); }
      }
    }
  };
  draw(job);
  pollMediaJob(job.id, (j) => { if (j.phase !== 'ready') draw(j); }, { signal: abort && abort.signal })
    .then((final) => { if (final) draw(final); })
    .catch((e) => { if (e.name !== 'AbortError') card.append(errorBox(e)); });
}

function render() {
  clear(root);
  const tablist = h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Create' });
  for (const t of TABS) {
    const btn = h('button', {
      type: 'button', role: 'tab', id: `tab-${t.id}`, 'aria-selected': String(t.id === state.tab),
      'aria-controls': `panel-${t.id}`, tabindex: t.id === state.tab ? '0' : '-1', class: 'tab',
    }, t.label);
    btn.addEventListener('click', () => { state.tab = t.id; history.replaceState(null, '', `#/create/${t.id}`); render(); });
    btn.addEventListener('keydown', (ev) => {
      const i = TABS.findIndex((x) => x.id === state.tab);
      if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
        const n = (i + (ev.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length;
        state.tab = TABS[n].id; render();
        document.getElementById(`tab-${state.tab}`).focus();
      }
    });
    tablist.append(btn);
  }
  const tab = TABS.find((t) => t.id === state.tab) || TABS[0];
  root.append(
    h('p', { class: 'lead' }, 'Generate and edit images and videos on gx10-02. Every result is saved to the ',
      h('a', { href: '#/library' }, 'Media Library'), '; originals are never overwritten.'),
    tablist,
    h('div', { class: 'tab-panel', role: 'tabpanel', id: `panel-${tab.id}`, 'aria-labelledby': `tab-${tab.id}` }, formFor(tab)),
    h('div', { id: 'job-panel', class: 'job-panel' }));
  api.get('/api/media/jobs').then((data) => {
    for (const j of data.jobs.slice(0, 6).reverse()) track(j);
  }).catch(() => {});
}

export default {
  title: 'Create',
  interval: 0,
  async mount(el, { params }) {
    clear(el);
    root = el;
    abort = new AbortController();
    const wanted = ((params && params[0]) || '').split('?')[0];
    state = { tab: TABS.some((t) => t.id === wanted) ? wanted : 'image', source: null };
    options = await api.get('/api/media/options');
    const pre = new URLSearchParams(location.hash.split('?')[1] || '');
    if (pre.get('source')) {
      try { state.source = await api.get(`/api/media/assets/${pre.get('source')}`); } catch { /* ignore */ }
    }
    render();
  },
  unmount() { if (abort) abort.abort(); },
};
