// Wan 2.2 LoRAs for the Video page (Build V3 WAN): the LoRA stack, the LoRA
// library, presets, the advanced workflow view, video history and errors.
// Every name shown here comes from the server; the browser only ever sends
// library entry ids, strengths and choices.
import { api, cached, invalidate, qs } from './api.js';
import { detailsDrawer } from './assets.js';
import { ago, bytes, clear, confirmDialog, dateTime, debounce, h, openDialog, openDrawer, promptDialog, toast, truncate, replace } from './dom.js';
import { center, isTerminal, jobKind } from './jobs.js';
import { badge, button, callout, emptyState, field, iconButton, kv, linkButton, loading, select, slider, textInput, toggle } from './ui.js';

export const getVideoConfig = () => cached('video-config', () => api.get('/api/video/config'), 120_000);
export const getLoraLibrary = (refresh = false) => api.get(`/api/video/loras${refresh ? '?refresh=1' : ''}`);
export const getPresets = () => api.get('/api/video/presets');
export const generateVideo = (body) => api.post('/api/video/generate', body);
export const previewWorkflow = (body) => api.post('/api/video/workflow', body, { timeout: 60_000 });

const APPLY_LABEL = { pair: 'High + low (paired)', high: 'High-noise expert only', low: 'Low-noise expert only', both: 'Both experts (explicit)' };
const STATE_LABEL = {
  paired: ['Paired', 'ok'], high_only: ['High only', 'info'], low_only: ['Low only', 'info'], general: ['General', 'info'],
  unresolved: ['Unresolved', 'warn'], broken_high: ['High file missing', 'danger'], broken_low: ['Low file missing', 'danger'],
};
const COMPAT = { compatible: ['Compatible', 'ok'], incompatible: ['Incompatible', 'danger'], unknown: ['Unknown compatibility', 'warn'] };
const STATUS = {
  queued: ['Queued', 'neutral'], waiting: ['Waiting', 'warn'], generating: ['Generating', 'info'], saving: ['Saving', 'info'],
  ready: ['Complete', 'ok'], failed: ['Failed', 'danger'], cancelled: ['Cancelled', 'neutral'],
};

const fmt = (v) => (v === null || v === undefined ? '—' : Number(v).toFixed(2));
const appliesHigh = (it) => ['pair', 'high', 'both'].includes(it.apply);
const appliesLow = (it) => ['pair', 'low', 'both'].includes(it.apply);

export function entryBadges(e) {
  const [sl, st] = STATE_LABEL[e.pair_state] || [e.pair_state, 'neutral'];
  const [cl, ct] = COMPAT[e.compatibility] || [e.compatibility, 'neutral'];
  return [badge(sl, st), badge(cl, ct), e.enabled === false ? badge('Disabled', 'neutral') : null,
    e.pair_source === 'manual' ? badge('Manual pair', 'neutral') : null];
}

// Turns a library entry into a stack item with explicit, visible strengths.
export function itemFromEntry(entry, defaults) {
  const single = entry.apply_options.length === 1 ? entry.apply_options[0] : null;
  return {
    entry_id: entry.id, display_name: entry.display_name, kind: entry.kind, apply: single, enabled: true,
    strength_high: entry.default_high ?? defaults.strength_high,
    strength_low: entry.default_low ?? defaults.strength_low,
    high_file: entry.high_file, low_file: entry.low_file, file: entry.file,
    apply_options: entry.apply_options, problems: entry.problems || [], usable: entry.usable, missing: false,
  };
}

// Stack items -> the request's `loras` list (only what the server accepts).
export function stackPayload(items) {
  return items.map((it) => {
    const out = { entry_id: it.entry_id, enabled: it.enabled, apply: it.apply || null };
    if (appliesHigh(it) || !it.apply) out.strength_high = it.strength_high;
    if (appliesLow(it) || !it.apply) out.strength_low = it.strength_low;
    for (const k of ['high_file', 'low_file', 'file', 'display_name']) if (it[k]) out[k] = it[k];
    return out;
  });
}

// ============================================================ LoRA stack
export function createLoraStack({ onChange } = {}) {
  let items = [];
  let defaults = { strength_high: 0.8, strength_low: 0.8, strength_min: 0, strength_max: 1.5, strength_step: 0.05, multi_lora_strength: 0.5, max_stack: 8 };
  let balanceDismissed = false;
  let library = null;
  const list = h('ol', { class: 'wan-stack', 'aria-label': 'LoRAs in application order' });
  const balance = h('div', { class: 'wan-balance' });
  const count = h('span', { class: 'muted small', 'aria-live': 'polite' });
  const addBtn = button('Add LoRA', { icon: 'plus', size: 'sm', attrs: { 'data-action': 'add-lora' }, onClick: () => openLibrary() });
  const libBtn = button('LoRA library', { icon: 'layers', size: 'sm', variant: 'ghost', attrs: { 'data-action': 'open-lora-library' }, onClick: () => openLibrary({ manage: true }) });
  const el = h('section', { class: 'panel-section wan-loras', 'aria-labelledby': 'wan-loras-h' },
    h('div', { class: 'row-between' }, h('h2', { class: 'field-label', id: 'wan-loras-h' }, 'LoRAs'), count),
    h('p', { class: 'field-hint' }, 'Applied in order. High-noise files go to the high-noise expert, low-noise files to the low-noise expert.'),
    list, balance, h('div', { class: 'row-wrap' }, addBtn, libBtn));

  const changed = () => { render(); if (onChange) onChange(items); };
  const enabledCount = () => items.filter((i) => i.enabled).length;

  async function openLibrary({ manage = false } = {}) {
    await openLoraLibrary({
      defaults,
      inStack: new Set(items.map((i) => i.entry_id)),
      onAdd: manage ? null : (entry) => {
        if (items.length >= defaults.max_stack) { toast(`At most ${defaults.max_stack} LoRAs.`, 'warn'); return false; }
        items.push(itemFromEntry(entry, defaults));
        changed();
        toast(`${entry.display_name} added.`, 'ok', 2000);
        return true;
      },
      onLibrary: (lib) => { library = lib; refreshFromLibrary(); },
    });
    addBtn.focus();
  }

  function refreshFromLibrary() {
    if (!library) return;
    const byId = new Map(library.entries.map((e) => [e.id, e]));
    for (const it of items) {
      const e = byId.get(it.entry_id);
      it.missing = !e;
      if (e) Object.assign(it, { display_name: e.display_name, apply_options: e.apply_options, problems: e.problems, usable: e.usable, kind: e.kind, high_file: e.high_file, low_file: e.low_file, file: e.file });
    }
    render();
  }

  function move(i, delta) {
    const j = i + delta;
    if (j < 0 || j >= items.length) return;
    [items[i], items[j]] = [items[j], items[i]];
    changed();
    const target = list.querySelector(`[data-index="${j}"] [data-action="${delta < 0 ? 'move-up' : 'move-down'}"]`)
      || list.querySelector(`[data-index="${j}"] [data-action="move-up"], [data-index="${j}"] [data-action="move-down"]`);
    if (target && !target.disabled) target.focus();
  }

  function strengthSlider(it, which) {
    const key = which === 'high' ? 'strength_high' : 'strength_low';
    const s = slider({
      label: which === 'high' ? 'High noise' : 'Low noise', min: defaults.strength_min, max: defaults.strength_max,
      step: defaults.strength_step, value: it[key], format: fmt,
      onInput: (v) => { it[key] = Math.round(v * 100) / 100; if (onChange) onChange(items); },
    });
    s.input.setAttribute('aria-label', `${it.display_name}: ${which}-noise strength`);
    s.input.dataset.strength = which;
    return s;
  }

  function row(it, i) {
    const name = it.display_name;
    const applySel = it.apply_options && it.apply_options.length > 1
      ? select([['', 'Choose where it applies…'], ...it.apply_options.map((o) => [o, APPLY_LABEL[o]])], it.apply || '', {
        attrs: { 'aria-label': `${name}: where it applies`, 'data-field': 'apply' },
        onChange: (v) => { it.apply = v || null; changed(); },
      }) : null;
    const sw = toggle('Enabled', it.enabled, { onChange: (on) => { it.enabled = on; changed(); } });
    sw.input.setAttribute('aria-label', `${name}: enabled`);
    const warn = it.missing ? 'This LoRA is no longer in the library. Remove it or rescan.'
      : (!it.usable && it.problems && it.problems.length ? it.problems.join(' · ') : null);
    return h('li', { class: `wan-item${it.enabled ? '' : ' is-off'}`, dataset: { index: String(i), entry: it.entry_id } },
      h('div', { class: 'wan-item-head' },
        h('span', { class: 'wan-order', 'aria-hidden': 'true' }, String(i + 1)),
        h('div', { class: 'wan-item-name' },
          h('p', { class: 'wan-name' }, name),
          h('p', { class: 'muted small wan-files' }, [it.high_file, it.low_file, it.apply === 'both' ? it.file : null].filter(Boolean).map((n) => n.split('/').pop()).join(' · ') || (it.file || ''))),
        h('div', { class: 'wan-item-tools', role: 'group', 'aria-label': `${name} controls` },
          iconButton('chevronDown', `Move ${name} up`, () => move(i, -1), { attrs: { 'data-action': 'move-up', class: 'icon-btn icon-btn-ghost wan-up', disabled: i === 0 } }),
          iconButton('chevronDown', `Move ${name} down`, () => move(i, 1), { attrs: { 'data-action': 'move-down', disabled: i === items.length - 1 } }),
          iconButton('x', `Remove ${name}`, () => { items.splice(i, 1); changed(); (list.querySelector('[data-action="move-up"]') || addBtn).focus(); }, { attrs: { 'data-action': 'remove' } }))),
      h('div', { class: 'wan-item-body' },
        sw,
        applySel ? field('Applies to', applySel) : h('p', { class: 'muted small' }, APPLY_LABEL[it.apply] || ''),
        (appliesHigh(it) || !it.apply) && it.apply !== null ? strengthSlider(it, 'high') : null,
        (appliesLow(it) || !it.apply) && it.apply !== null ? strengthSlider(it, 'low') : null,
        warn ? h('p', { class: 'form-error form-danger small', role: 'status' }, warn) : null));
  }

  function render() {
    replace(list, items.length ? items.map(row) : h('li', { class: 'muted small wan-empty' }, 'No LoRAs: the standard Wan 2.2 workflow runs as is.'));
    count.textContent = items.length ? `${enabledCount()} of ${items.length} enabled` : '';
    clear(balance);
    const enabled = items.filter((i) => i.enabled);
    const target = defaults.multi_lora_strength;
    const alreadyBalanced = enabled.every((i) => (!appliesHigh(i) || i.strength_high === target) && (!appliesLow(i) || i.strength_low === target));
    if (enabled.length >= 2 && !balanceDismissed && !alreadyBalanced) {
      balance.append(callout('info', 'Several LoRAs are enabled',
        `Stacked LoRAs add up. A starting point is about ${target.toFixed(1)} each; your values stay unless you choose this.`, [
          button(`Use ${target.toFixed(1)} for each`, { size: 'sm', attrs: { 'data-action': 'balance' }, onClick: () => {
            for (const it of enabled) { if (appliesHigh(it)) it.strength_high = target; if (appliesLow(it)) it.strength_low = target; }
            changed();
            toast(`Enabled LoRAs set to ${target.toFixed(1)}.`, 'ok', 2500);
          } }),
          button('Keep my values', { size: 'sm', variant: 'ghost', onClick: () => { balanceDismissed = true; render(); } }),
        ]));
    }
  }

  el.setDefaults = (d) => { defaults = { ...defaults, ...d }; render(); };
  el.get = () => items.map((i) => ({ ...i }));
  el.payload = () => stackPayload(items);
  el.set = (next) => { items = (next || []).map((i) => ({ enabled: true, apply_options: [], problems: [], usable: true, missing: false, ...i })); balanceDismissed = false; render(); };
  el.hydrate = async () => {
    try { library = await getLoraLibrary(); refreshFromLibrary(); } catch { /* shown when the library opens */ }
  };
  el.validate = () => {
    const pending = items.find((i) => i.enabled && !i.apply);
    if (pending) return `Choose where “${pending.display_name}” applies (high noise, low noise or both).`;
    const missing = items.find((i) => i.enabled && i.missing);
    if (missing) return `“${missing.display_name}” is no longer in the LoRA library. Remove it or rescan.`;
    return null;
  };
  render();
  return el;
}

// ============================================================ LoRA library
const FILTERS = [
  ['all', 'All LoRAs'], ['usable', 'Ready to use'], ['paired', 'Paired'], ['high_only', 'High noise only'], ['low_only', 'Low noise only'],
  ['general', 'General'], ['unresolved', 'Unresolved'], ['compatible', 'Compatible'], ['incompatible', 'Incompatible'],
  ['unknown', 'Unknown compatibility'], ['disabled', 'Disabled'],
];
const SORTS = [['order', 'Library order'], ['name', 'Name'], ['discovered', 'Newest first'], ['size', 'Largest first']];

function matches(e, filter, q) {
  if (q) {
    const hay = [e.display_name, e.high_file, e.low_file, e.file, (e.tags || []).join(' '), e.description].join(' ').toLowerCase();
    if (!hay.includes(q)) return false;
  }
  switch (filter) {
    case 'usable': return e.usable && e.enabled;
    case 'paired': return e.pair_state === 'paired';
    case 'high_only': case 'low_only': case 'general': return e.pair_state === filter;
    case 'unresolved': return e.unresolved;
    case 'compatible': case 'incompatible': case 'unknown': return e.compatibility === filter;
    case 'disabled': return !e.enabled;
    default: return true;
  }
}

function fileDetails(f) {
  const meta = Object.entries(f.metadata || {});
  return h('div', { class: 'wan-file' },
    h('p', { class: 'wan-file-name' }, f.name),
    kv([
      ['Path on gx10-02', h('code', {}, f.path)],
      ['Size', bytes(f.size)], ['Modified', dateTime(f.mtime)],
      ['Noise class', `${f.noise}${f.noise_reason ? ` (${f.noise_reason})` : ''}`],
      ['Compatibility', `${f.compatibility}: ${f.compatibility_reason || ''}`],
      ['Family', f.family], ['Format', f.key_format], ['Tensors', f.tensors], ['Rank', f.rank],
      ['Hidden size', f.hidden_dim], ['Blocks', f.blocks],
      ['ComfyUI lists it', f.comfy_visible === null ? 'not checked' : (f.comfy_visible ? 'yes' : 'no (rescan)')],
      ['Valid file', f.valid ? 'yes' : `no: ${f.error}`],
      ['Header SHA-256', f.header_sha256 ? truncate(f.header_sha256, 20) : null],
    ]),
    meta.length ? h('details', { class: 'err-detail' }, h('summary', {}, `Header metadata (${meta.length})`),
      kv(meta.map(([k, v]) => [k, v]))) : h('p', { class: 'muted small' }, 'No header metadata.'));
}

function settingsForm(e, onSaved) {
  const name = textInput({ value: e.display_name, maxLength: 120 });
  const desc = h('textarea', { class: 'input', rows: 2, maxlength: 1000 });
  desc.value = e.description || '';
  const tags = textInput({ value: (e.tags || []).join(', '), maxLength: 500, placeholder: 'comma separated' });
  const hi = h('input', { class: 'input', type: 'number', min: 0, max: 1.5, step: 0.05, placeholder: 'default 0.8', inputmode: 'decimal' });
  const lo = h('input', { class: 'input', type: 'number', min: 0, max: 1.5, step: 0.05, placeholder: 'default 0.8', inputmode: 'decimal' });
  if (e.default_high !== null && e.default_high !== undefined) hi.value = String(e.default_high);
  if (e.default_low !== null && e.default_low !== undefined) lo.value = String(e.default_low);
  const enabled = toggle('Available for selection', e.enabled);
  const unknown = toggle('Allow unknown compatibility', e.allow_unknown, { hint: 'Only for files you know work with Wan 2.2 T2V-A14B.' });
  const err = h('p', { class: 'form-error form-danger', role: 'alert', hidden: true });
  const num = (input) => (input.value.trim() === '' ? null : Number(input.value));
  const save = button('Save settings', { size: 'sm', variant: 'primary', attrs: { 'data-action': 'save-entry' }, onClick: async () => {
    err.hidden = true;
    const body = {
      display_name: name.value.trim(), description: desc.value.trim(),
      tags: tags.value.split(',').map((t) => t.trim()).filter(Boolean),
      default_high: num(hi), default_low: num(lo), enabled: enabled.input.checked,
      allow_unknown: unknown.input.checked,
    };
    for (const [k, v] of [['High default', body.default_high], ['Low default', body.default_low]]) {
      if (v !== null && (!Number.isFinite(v) || v < 0 || v > 1.5)) { err.textContent = `${k} must be between 0.0 and 1.5.`; err.hidden = false; return; }
    }
    try {
      const next = await api.post(`/api/video/loras/${e.id}`, body);
      toast('Saved.', 'ok', 2000);
      onSaved(next);
    } catch (ex) { err.textContent = ex.message; err.hidden = false; }
  } });
  return h('div', { class: 'stack-sm wan-settings' },
    field('Display name', name), field('Description', desc), field('Tags', tags),
    h('div', { class: 'grid-2' }, field('Default high strength', hi), field('Default low strength', lo)),
    enabled, unknown, err, h('div', { class: 'row-wrap' }, save));
}

export async function openLoraLibrary({ defaults = {}, inStack = new Set(), onAdd = null, onLibrary = null } = {}) {
  const state = { lib: null, q: '', filter: 'all', sort: 'order', open: new Set() };
  const status = h('p', { class: 'muted small', 'aria-live': 'polite' });
  const listEl = h('ul', { class: 'wan-lib', 'aria-label': 'LoRA library' });
  const extras = h('div', { class: 'stack' });
  const search = textInput({ placeholder: 'Search name, file or tag', attrs: { type: 'search', 'aria-label': 'Search LoRAs', id: 'wan-lib-search' } });
  const filterSel = select(FILTERS, 'all', { attrs: { 'aria-label': 'Filter LoRAs' }, onChange: (v) => { state.filter = v; render(); } });
  const sortSel = select(SORTS, 'order', { attrs: { 'aria-label': 'Sort LoRAs' }, onChange: (v) => { state.sort = v; render(); } });
  const rescanBtn = button('Rescan', { icon: 'refresh', size: 'sm', attrs: { 'data-action': 'rescan' }, onClick: () => rescan() });
  search.addEventListener('input', debounce(() => { state.q = search.value.trim().toLowerCase(); render(); }, 150));
  const body = h('div', { class: 'stack wan-library' },
    h('div', { class: 'row-between' }, status, rescanBtn),
    h('div', { class: 'filters-row wan-filters' }, search, filterSel, sortSel),
    listEl, extras);
  const dlg = openDrawer({ title: onAdd ? 'Add a LoRA' : 'LoRA library', body, wide: true });
  replace(listEl, h('li', {}, loading('Reading the LoRA library…')));

  async function load(refresh = false) {
    try {
      state.lib = await getLoraLibrary(refresh);
      if (onLibrary) onLibrary(state.lib);
      render();
    } catch (err) {
      replace(listEl, h('li', {}, emptyState({ icon: 'alert', title: 'The LoRA library is not available', text: err.message,
        action: button('Try again', { size: 'sm', onClick: () => load(true) }) })));
    }
  }

  async function rescan() {
    rescanBtn.disabled = true;
    status.textContent = 'Rescanning gx10-02…';
    try {
      state.lib = await api.post('/api/video/loras/rescan', {}, { timeout: 300_000 });
      if (onLibrary) onLibrary(state.lib);
      render();
      toast('LoRA library rescanned.', 'ok', 2500);
    } catch (err) {
      toast(err.message, 'danger');
      status.textContent = err.message;
    } finally { rescanBtn.disabled = false; }
  }

  async function reorder(ids) {
    try { state.lib = await api.post('/api/video/loras/order', { ids }); render(); } catch (err) { toast(err.message, 'danger'); }
  }

  function sorted(entries) {
    const list = entries.filter((e) => matches(e, state.filter, state.q));
    const by = {
      name: (a, b) => a.display_name.localeCompare(b.display_name),
      discovered: (a, b) => (b.discovered_at || 0) - (a.discovered_at || 0),
      size: (a, b) => b.size - a.size,
    }[state.sort];
    return by ? [...list].sort(by) : list;
  }

  function card(e, index, all) {
    const open = state.open.has(e.id);
    const added = inStack.has(e.id);
    const actions = [
      onAdd ? button(added ? 'Added' : 'Add', { icon: added ? 'check' : 'plus', size: 'sm', variant: 'primary', attrs: { 'data-action': 'add', disabled: !e.usable || !e.enabled || added, 'aria-label': `Add ${e.display_name}` }, onClick: (ev) => {
        if (onAdd(e)) { inStack.add(e.id); ev.currentTarget.disabled = true; }
      } }) : null,
      button(open ? 'Hide details' : 'Details', { size: 'sm', variant: 'ghost', attrs: { 'aria-expanded': String(open), 'data-action': 'details', 'aria-label': `${open ? 'Hide' : 'Show'} details for ${e.display_name}` }, onClick: () => { if (open) state.open.delete(e.id); else state.open.add(e.id); render(); listEl.querySelector(`[data-entry="${e.id}"] [data-action="details"]`)?.focus(); } }),
      e.kind === 'pair' ? button('Unpair', { size: 'sm', variant: 'ghost', attrs: { 'data-action': 'unpair', 'aria-label': `Unpair ${e.display_name}` }, onClick: async () => {
        const ok = await confirmDialog({ title: 'Unpair these files?', message: `${e.high_file} and ${e.low_file} will be listed separately. You can pair them again later.`, okLabel: 'Unpair' });
        if (!ok) return;
        try { state.lib = await api.post('/api/video/pairs/remove', { entry_id: e.id }); if (onLibrary) onLibrary(state.lib); render(); toast('Unpaired.', 'ok', 2000); } catch (err) { toast(err.message, 'danger'); }
      } }) : null,
      state.sort === 'order' && !state.q && state.filter === 'all' ? iconButton('chevronDown', `Move ${e.display_name} up`, () => {
        const ids = all.map((x) => x.id); [ids[index - 1], ids[index]] = [ids[index], ids[index - 1]]; reorder(ids).then(() => listEl.querySelector(`[data-entry="${e.id}"] .wan-up`)?.focus());
      }, { attrs: { class: 'icon-btn icon-btn-ghost wan-up', disabled: index === 0 } }) : null,
      state.sort === 'order' && !state.q && state.filter === 'all' ? iconButton('chevronDown', `Move ${e.display_name} down`, () => {
        const ids = all.map((x) => x.id); [ids[index + 1], ids[index]] = [ids[index], ids[index + 1]]; reorder(ids).then(() => listEl.querySelector(`[data-entry="${e.id}"] .wan-down`)?.focus());
      }, { attrs: { class: 'icon-btn icon-btn-ghost wan-down', disabled: index === all.length - 1 } }) : null,
    ];
    const files = [e.high_file ? ['High noise', e.high_file] : null, e.low_file ? ['Low noise', e.low_file] : null,
      !e.high_file && !e.low_file && e.file ? ['File', e.file] : null].filter(Boolean);
    return h('li', { class: 'wan-entry', dataset: { entry: e.id } },
      h('div', { class: 'wan-entry-top' },
        h('div', { class: 'wan-entry-main' },
          h('p', { class: 'wan-name' }, e.display_name),
          h('div', { class: 'row-wrap' }, entryBadges(e)),
          h('dl', { class: 'kv wan-kv' }, files.flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]),
            h('dt', {}, 'Size'), h('dd', {}, bytes(e.size)),
            h('dt', {}, 'Discovered'), h('dd', {}, e.discovered_at ? ago(e.discovered_at) : '—'),
            h('dt', {}, 'Default strengths'), h('dd', {}, `high ${fmt(e.default_high ?? defaults.strength_high)} · low ${fmt(e.default_low ?? defaults.strength_low)}`)),
          e.tags && e.tags.length ? h('p', { class: 'muted small' }, `Tags: ${e.tags.join(', ')}`) : null,
          e.problems && e.problems.length ? h('ul', { class: 'wan-problems small' }, e.problems.map((p) => h('li', {}, p))) : null),
        h('div', { class: 'wan-entry-actions' }, actions)),
      open ? h('div', { class: 'wan-entry-details' },
        e.description ? h('p', {}, e.description) : null,
        ...e.files.map(fileDetails),
        settingsForm(e, (next) => { const i = state.lib.entries.findIndex((x) => x.id === e.id); state.lib.entries[i] = next; if (onLibrary) onLibrary(state.lib); render(); })) : null);
  }

  function pairTool() {
    const files = state.lib.unpaired_files || [];
    const highs = files.filter((f) => f.noise === 'high');
    const lows = files.filter((f) => f.noise === 'low');
    if (!highs.length || !lows.length) return null;
    const hs = select(highs.map((f) => [f.name, f.name]), highs[0].name, { attrs: { 'aria-label': 'High-noise file', id: 'wan-pair-high' } });
    const ls = select(lows.map((f) => [f.name, f.name]), lows[0].name, { attrs: { 'aria-label': 'Low-noise file', id: 'wan-pair-low' } });
    return h('section', { class: 'card wan-pair', 'aria-labelledby': 'wan-pair-h' },
      h('h3', { class: 'section-title', id: 'wan-pair-h' }, 'Pair files by hand'),
      h('p', { class: 'muted small' }, 'For a high-noise and a low-noise file that belong together but were not matched automatically. Files are never renamed.'),
      h('div', { class: 'grid-2' }, field('High-noise file', hs), field('Low-noise file', ls)),
      h('div', { class: 'row-wrap' }, button('Pair', { size: 'sm', variant: 'primary', attrs: { 'data-action': 'pair' }, onClick: async () => {
        try {
          await api.post('/api/video/pairs', { high_file: hs.value, low_file: ls.value });
          toast('Paired.', 'ok', 2000);
          await load(true);
        } catch (err) { toast(err.message, 'danger'); }
      } })));
  }

  function render() {
    const lib = state.lib;
    if (!lib) return;
    const all = lib.entries;
    const shown = sorted(all);
    const comfy = lib.comfy || {};
    status.textContent = `${all.length} LoRA${all.length === 1 ? '' : 's'} · scanned ${lib.scanned_at ? ago(lib.scanned_at) : 'never'}`
      + (comfy.error ? ` · ComfyUI: ${comfy.error}` : '') + (state.q || state.filter !== 'all' ? ` · ${shown.length} shown` : '');
    replace(listEl, shown.length ? shown.map((e) => card(e, all.indexOf(e), all))
      : h('li', {}, emptyState({ icon: 'layers', title: all.length ? 'No LoRA matches' : 'No LoRAs found',
        text: all.length ? 'Change the search or the filter.' : `Put .safetensors files under ${((lib.roots || []).find((r) => r.label === 'video') || {}).path || 'the video LoRA folder'}/wan22/ on gx10-02, then press Rescan.` })));
    replace(extras,
      pairTool(),
      lib.missing_files && lib.missing_files.length ? h('details', { class: 'disclosure' }, h('summary', {}, h('span', {}, `Missing files (${lib.missing_files.length})`)),
        h('ul', { class: 'disclosure-body small' }, lib.missing_files.map((m) => h('li', {}, `${m.name} · last seen ${ago(m.last_seen)}`)))) : null,
      lib.problems && lib.problems.length ? callout('warn', 'Scan notes', h('ul', {}, lib.problems.map((p) => h('li', {}, p)))) : null,
      h('details', { class: 'disclosure' }, h('summary', {}, h('span', {}, 'Where LoRA files live')),
        h('div', { class: 'disclosure-body small stack-sm' },
          h('p', {}, 'ComfyUI reads LoRAs from these folders on gx10-02 (subfolders included):'),
          h('ul', {}, (lib.roots || []).map((r) => h('li', {}, h('code', {}, r.path)))),
          h('p', {}, 'Suggested layout: wan22/paired, wan22/high_noise, wan22/low_noise, wan22/general. Existing files can stay where they are.'))));
  }

  await load(false);
  search.focus();
  return dlg;
}

// ============================================================ presets
export function presetPicker({ getCurrent, onApply }) {
  let presets = [];
  const sel = select([['', 'No preset']], '', { attrs: { 'aria-label': 'Preset', id: 'wan-preset' } });
  const applyBtn = button('Apply', { size: 'sm', attrs: { 'data-action': 'apply-preset' }, onClick: () => {
    const p = presets.find((x) => x.id === sel.value);
    if (p) { onApply(p); toast(`Preset “${p.name}” applied. Everything stays editable.`, 'ok', 2500); }
  } });
  const saveBtn = button('Save as…', { size: 'sm', variant: 'ghost', attrs: { 'data-action': 'save-preset' }, onClick: () => saveAs() });
  const updateBtn = button('Update', { size: 'sm', variant: 'ghost', attrs: { 'data-action': 'update-preset' }, onClick: () => update() });
  const manageBtn = button('Manage', { size: 'sm', variant: 'ghost', attrs: { 'data-action': 'manage-presets' }, onClick: () => manage() });
  const el = h('section', { class: 'panel-section', 'aria-labelledby': 'wan-preset-h' },
    h('label', { class: 'field-label', id: 'wan-preset-h', for: 'wan-preset' }, 'Preset'),
    sel, h('div', { class: 'row-wrap' }, applyBtn, saveBtn, updateBtn, manageBtn));

  async function load(selectId) {
    try {
      presets = (await getPresets()).presets || [];
    } catch (err) { toast(err.message, 'danger'); return; }
    const keep = selectId ?? sel.value;
    replace(sel, h('option', { value: '' }, 'No preset'), presets.map((p) => h('option', { value: p.id }, p.builtin ? `${p.name} (example)` : p.name)));
    sel.value = presets.some((p) => p.id === keep) ? keep : '';
    sync();
  }
  function sync() { applyBtn.disabled = !sel.value; updateBtn.disabled = !sel.value; }
  sel.addEventListener('change', sync);

  async function saveAs() {
    const name = await promptDialog({ title: 'Save preset', label: 'Preset name', value: '', okLabel: 'Save', maxLength: 80 });
    if (!name) return;
    try {
      const p = await api.post('/api/video/presets', { name, data: getCurrent() });
      toast(`Preset “${p.name}” saved.`, 'ok');
      await load(p.id);
    } catch (err) { toast(err.message, 'danger'); }
  }
  async function update() {
    const p = presets.find((x) => x.id === sel.value);
    if (!p) return;
    const ok = await confirmDialog({ title: `Update “${p.name}”?`, message: 'The preset is replaced with the current form: prompt style, negative prompt, LoRAs, strengths and all generation settings.', okLabel: 'Update' });
    if (!ok) return;
    try { await api.post(`/api/video/presets/${p.id}`, { data: getCurrent() }); toast('Preset updated.', 'ok'); await load(p.id); } catch (err) { toast(err.message, 'danger'); }
  }
  function manage() {
    const listEl = h('ul', { class: 'wan-presets' });
    const renderList = () => replace(listEl, presets.map((p) => h('li', { class: 'wan-preset', dataset: { preset: p.id } },
      h('div', { class: 'wan-entry-main' },
        h('p', { class: 'wan-name' }, p.name, p.builtin ? ' ' : null, p.builtin ? badge('Example', 'neutral') : null),
        h('p', { class: 'muted small' }, p.description || `${p.data.size} · ${p.data.seconds} s · ${p.data.fps} fps · ${p.data.loras.length} LoRA(s)`),
        h('p', { class: 'muted small' }, `Preset id ${p.id}`)),
      h('div', { class: 'row-wrap' },
        button('Apply', { size: 'sm', attrs: { 'aria-label': `Apply ${p.name}` }, onClick: () => { onApply(p); sel.value = p.id; sync(); dlg.close(); } }),
        button('Rename', { size: 'sm', variant: 'ghost', attrs: { 'aria-label': `Rename ${p.name}` }, onClick: async () => {
          const name = await promptDialog({ title: 'Rename preset', label: 'Preset name', value: p.name, maxLength: 80 });
          if (!name || name === p.name) return;
          try { await api.post(`/api/video/presets/${p.id}`, { name }); await load(); renderList(); toast('Renamed.', 'ok', 2000); } catch (err) { toast(err.message, 'danger'); }
        } }),
        button('Duplicate', { size: 'sm', variant: 'ghost', attrs: { 'aria-label': `Duplicate ${p.name}` }, onClick: async () => {
          try { await api.post(`/api/video/presets/${p.id}/duplicate`, {}); await load(); renderList(); toast('Duplicated.', 'ok', 2000); } catch (err) { toast(err.message, 'danger'); }
        } }),
        button('Delete', { size: 'sm', variant: 'danger-ghost', attrs: { 'aria-label': `Delete ${p.name}` }, onClick: async () => {
          const ok = await confirmDialog({ title: `Delete “${p.name}”?`, message: 'This cannot be undone. Creative Flows that use this preset stop working.', okLabel: 'Delete', danger: true });
          if (!ok) return;
          try { await api.post(`/api/video/presets/${p.id}/delete`, { confirm: true }); await load(); renderList(); toast('Deleted.', 'ok', 2000); } catch (err) { toast(err.message, 'danger'); }
        } })))));
    renderList();
    const dlg = openDialog({ title: 'Video presets', body: h('div', { class: 'stack' }, h('p', { class: 'muted small' }, 'A preset stores LoRAs, strengths, size, length, frame rate, seed mode, prompt style, negative prompt and sampler settings.'), listEl), className: 'dialog-wide' });
  }

  el.load = load;
  el.selected = () => sel.value || null;
  load();
  return el;
}

// ============================================================ advanced view
function chainTable(chains) {
  const rows = [];
  for (const branch of ['high', 'low']) {
    for (const [i, c] of (chains[branch] || []).entries()) {
      rows.push(h('tr', {}, h('td', {}, branch === 'high' ? 'High noise' : 'Low noise'), h('td', {}, String(i + 1)),
        h('td', {}, h('code', {}, c.lora_name)), h('td', {}, fmt(c.strength)), h('td', {}, c.base ? 'built in' : `node ${c.node}`)));
    }
  }
  return h('div', { class: 'wan-table-wrap', tabindex: '0', role: 'region', 'aria-label': 'LoRA chains' },
    h('table', { class: 'wan-table' },
      h('caption', { class: 'sr-only' }, 'LoRAs per expert branch in application order'),
      h('thead', {}, h('tr', {}, ['Branch', 'Order', 'File', 'Strength', 'Node'].map((t) => h('th', { scope: 'col' }, t)))),
      h('tbody', {}, rows)));
}

export function advancedView(data, { downloadUrl } = {}) {
  const json = JSON.stringify(data.graph || data.workflow || {}, null, 2);
  const pre = h('pre', { class: 'wan-json', tabindex: '0', 'aria-label': 'Generated ComfyUI workflow JSON' }, json);
  const copy = button('Copy JSON', { icon: 'copy', size: 'sm', attrs: { 'data-action': 'copy-workflow' }, onClick: async () => {
    try { await navigator.clipboard.writeText(json); toast('Workflow copied.', 'ok', 2000); } catch {
      const range = document.createRange(); range.selectNodeContents(pre);
      const s = window.getSelection(); s.removeAllRanges(); s.addRange(range);
      toast('Copying is blocked here; the JSON is selected, press Ctrl+C.', 'warn');
    }
  } });
  const dl = downloadUrl
    ? linkButton('Download JSON', downloadUrl, { icon: 'download', size: 'sm', download: '', attrs: { 'data-action': 'download-workflow' } })
    : button('Download JSON', { icon: 'download', size: 'sm', attrs: { 'data-action': 'download-workflow' }, onClick: () => {
      const url = URL.createObjectURL(new Blob([`${json}\n`], { type: 'application/json' }));
      const a = h('a', { href: url, download: 'gx-wan22-workflow-preview.json' });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } });
  const chains = data.chains || {};
  return h('div', { class: 'stack wan-advanced' },
    kv([['Workflow', data.workflow || 'wan22-t2v-a14b-uncensored'], ['Workflow version', data.workflow_version],
      ['High-noise model', chains.high_model || data.high_model], ['Low-noise model', chains.low_model || data.low_model],
      ['ComfyUI prompt id', data.comfy_prompt_id], ['Seed', data.seed], ['Frames', data.frames]]),
    h('h3', { class: 'section-title' }, 'Application order'),
    chainTable(chains),
    h('h3', { class: 'section-title' }, 'Generated workflow'),
    h('div', { class: 'row-wrap' }, copy, dl),
    pre);
}

export function openAdvancedDialog(data, opts) {
  return openDialog({ title: 'Advanced view', body: advancedView(data, opts), className: 'dialog-wide' });
}

// ============================================================ history
export function generationDetails(gen, { onLoad } = {}) {
  const loras = gen.loras || [];
  const body = h('div', { class: 'stack' },
    gen.asset_id ? h('video', { class: 'media-video', src: gen.asset_url, controls: true, preload: 'metadata', poster: gen.thumbnail_url, 'aria-label': 'Generated video' }) : null,
    gen.error_message ? callout(gen.status === 'cancelled' ? 'info' : 'danger', gen.error_message, gen.error_detail ? h('details', { class: 'err-detail' }, h('summary', {}, 'Technical details'), h('pre', { class: 'wan-json' }, gen.error_detail)) : null) : null,
    h('div', { class: 'row-wrap' },
      onLoad ? button('Load into form', { icon: 'refresh', size: 'sm', variant: 'primary', attrs: { 'data-action': 'load-generation' }, onClick: () => { onLoad(gen); dlg.close(); } }) : null,
      gen.asset_id ? button('Asset metadata', { icon: 'info', size: 'sm', attrs: { 'data-action': 'asset-metadata' }, onClick: () => detailsDrawer(gen.asset_id) }) : null,
      gen.asset_id ? linkButton('Reuse in Creative Flows', gen.flows_url, { icon: 'flow', size: 'sm', attrs: { 'data-action': 'reuse-in-flows' } }) : null),
    kv([['Status', (STATUS[gen.status] || [gen.status])[0]], ['Created', dateTime(gen.created_at)], ['Prompt', gen.prompt],
      ['Negative prompt', gen.negative_prompt], ['Seed', gen.seed], ['Model', gen.model], ['Size', gen.size],
      ['Frames', gen.frames], ['FPS', gen.fps], ['Length', gen.seconds ? `${gen.seconds} s` : null],
      ['Sampler', gen.settings && gen.settings.sampler_name ? `${gen.settings.sampler_name} / ${gen.settings.scheduler} · ${gen.settings.steps} steps (switch at ${gen.settings.boundary}) · shift ${gen.settings.shift} · cfg ${gen.settings.cfg}` : null],
      ['Preset', gen.preset_id], ['Execution time', gen.duration_seconds ? `${Number(gen.duration_seconds).toFixed(1)} s` : null],
      ['Output', gen.output_path], ['Asset id', gen.asset_id], ['Router job', gen.router_job], ['Generation id', gen.id]]),
    h('h3', { class: 'section-title' }, `LoRAs (${loras.length})`),
    loras.length ? h('ul', { class: 'wan-hist-loras' }, loras.map((l) => h('li', {},
      `${l.order + 1}. ${l.display_name} — ${APPLY_LABEL[l.apply] || l.apply || 'not applied'}`,
      l.strength_high !== null && l.strength_high !== undefined ? ` · high ${fmt(l.strength_high)}` : '',
      l.strength_low !== null && l.strength_low !== undefined ? ` · low ${fmt(l.strength_low)}` : '',
      l.enabled ? '' : ' · disabled'))) : h('p', { class: 'muted small' }, 'No LoRAs.'),
    gen.workflow ? h('details', { class: 'disclosure' }, h('summary', {}, h('span', {}, 'Advanced view: chains and workflow JSON')),
      h('div', { class: 'disclosure-body' }, advancedView({ ...gen, graph: gen.workflow }, { downloadUrl: gen.workflow_url }))) : h('p', { class: 'muted small' }, 'No workflow was stored (the job never reached the media router).'));
  const dlg = openDrawer({ title: 'Video generation', body, wide: true });
  return dlg;
}

export function createHistoryPanel({ onLoad, onPlay }) {
  const state = { q: '', status: '', offset: 0, items: [], total: 0 };
  const listEl = h('ul', { class: 'wan-history', 'aria-label': 'Video generation history' });
  const search = textInput({ placeholder: 'Search prompts and LoRAs', attrs: { type: 'search', 'aria-label': 'Search video history' } });
  const statusSel = select([['', 'All'], ['active', 'In progress'], ['ready', 'Complete'], ['failed', 'Failed'], ['cancelled', 'Cancelled']], '', { attrs: { 'aria-label': 'Filter by status' }, onChange: (v) => { state.status = v; load(); } });
  const more = button('Load more', { size: 'sm', variant: 'ghost', onClick: () => load(true) });
  search.addEventListener('input', debounce(() => { state.q = search.value.trim(); load(); }, 250));
  const el = h('section', { class: 'ws-section wan-history-section', 'aria-labelledby': 'wan-history-h' },
    h('div', { class: 'row-between' }, h('h2', { class: 'section-title', id: 'wan-history-h' }, 'Video history'),
      h('div', { class: 'row-wrap' }, search, statusSel)),
    listEl, more);

  async function load(append = false) {
    if (!append) state.offset = 0;
    try {
      const res = await api.get(`/api/video/generations${qs({ q: state.q, status: state.status, limit: 12, offset: state.offset })}`);
      state.items = append ? [...state.items, ...res.items] : res.items;
      state.total = res.total;
      state.offset = state.items.length;
      render();
    } catch (err) {
      replace(listEl, h('li', {}, callout('danger', 'History is not available', err.message)));
    }
  }

  async function open(id) {
    try { generationDetails(await api.get(`/api/video/generations/${id}`), { onLoad }); } catch (err) { toast(err.message, 'danger'); }
  }

  function row(g) {
    const [label, tone] = STATUS[g.status] || [g.status, 'neutral'];
    const loras = (g.loras || []).filter((l) => l.enabled);
    return h('li', { class: 'wan-gen', dataset: { generation: g.id } },
      h('div', { class: 'lib-row-thumb' }, g.asset_id ? h('img', { class: 'thumb-img', src: g.thumbnail_url, alt: '', loading: 'lazy' }) : h('div', { class: 'thumb-ph thumb-ph-video' })),
      h('div', { class: 'lib-row-main' },
        h('p', { class: 'wan-name' }, truncate(g.title || g.prompt, 90)),
        h('p', { class: 'muted small' }, [ago(g.created_at), g.size, g.frames ? `${g.frames} frames` : null, g.fps ? `${g.fps} fps` : null, `seed ${g.seed}`,
          g.duration_seconds ? `${Number(g.duration_seconds).toFixed(0)} s` : null].filter(Boolean).join(' · ')),
        h('div', { class: 'row-wrap' }, badge(label, tone), loras.map((l) => badge(`${l.display_name} ${[l.strength_high, l.strength_low].filter((x) => x !== null && x !== undefined).map(fmt).join('/')}`, 'info'))),
        g.error_message ? h('p', { class: 'form-danger small' }, g.error_message) : null,
        g.live && g.live.waiting ? h('p', { class: 'muted small' }, `Waiting: ${g.live.waiting.reason || ''}`) : null),
      h('div', { class: 'lib-row-actions' },
        g.asset_id ? iconButton('play', 'Play in viewer', () => onPlay(g), { attrs: { 'data-action': 'play' } }) : null,
        iconButton('refresh', 'Load settings into the form', async () => {
          try { onLoad(await api.get(`/api/video/generations/${g.id}`)); } catch (err) { toast(err.message, 'danger'); }
        }, { attrs: { 'data-action': 'load' } }),
        iconButton('copy', 'Run again with the same settings', async () => {
          try {
            const full = await api.get(`/api/video/generations/${g.id}`);
            const job = await generateVideo(full.request);
            center.track(job);
            toast('Submitted again.', 'ok');
          } catch (err) { toast(err.message, 'danger'); }
        }, { attrs: { 'data-action': 'duplicate' } }),
        iconButton('info', 'Details, workflow and metadata', () => open(g.id), { attrs: { 'data-action': 'open' } })));
  }

  function render() {
    replace(listEl, state.items.length ? state.items.map(row) : h('li', {}, emptyState({ icon: 'clock', title: 'No video generations yet', text: 'Generated videos are listed here with their LoRAs and settings.' })));
    more.hidden = state.items.length >= state.total;
  }

  const unsub = center.subscribe((job) => { if (job && jobKind(job) === 'video' && isTerminal(job)) load(); });
  el.load = load;
  el.destroy = unsub;
  load();
  return el;
}

export function createErrorsPanel({ onLoad }) {
  const listEl = h('ul', { class: 'wan-errors', 'aria-label': 'Recent video errors' });
  const el = h('section', { class: 'ws-section', 'aria-labelledby': 'wan-errors-h' },
    h('details', { class: 'disclosure' },
      h('summary', {}, h('span', { id: 'wan-errors-h' }, 'Errors')),
      h('div', { class: 'disclosure-body' }, listEl)));
  async function load() {
    try {
      const res = await api.get('/api/video/errors?limit=20');
      replace(listEl, res.items.length ? res.items.map((e) => h('li', { class: 'err-row' },
        h('p', {}, h('strong', {}, e.error_message || 'Failed'), ' ', badge(e.error_code || 'error', e.status === 'cancelled' ? 'neutral' : 'danger')),
        h('p', { class: 'muted small' }, `${dateTime(e.created_at)} · ${truncate(e.prompt, 80)}`),
        e.error_detail ? h('details', { class: 'err-detail' }, h('summary', {}, 'Technical details'), h('pre', { class: 'wan-json' }, e.error_detail)) : null,
        button('Load settings', { size: 'sm', variant: 'ghost', onClick: async () => {
          try { onLoad(await api.get(`/api/video/generations/${e.id}`)); } catch (err) { toast(err.message, 'danger'); }
        } }))) : h('li', { class: 'muted small' }, 'No failed or cancelled video generations.'));
    } catch (err) {
      replace(listEl, h('li', { class: 'form-danger' }, err.message));
    }
  }
  const unsub = center.subscribe((job) => { if (job && jobKind(job) === 'video' && isTerminal(job)) load(); });
  el.destroy = unsub;
  load();
  return el;
}

export function invalidateVideoConfig() { invalidate('video-config'); }
