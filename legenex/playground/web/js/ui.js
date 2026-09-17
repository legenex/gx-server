// Reusable UI components: buttons, fields, chips, tabs, sliders, seed input,
// prompt composer, dropzone, skeletons, empty states, media thumbnails.
import { h, uid, randomSeed, storeGet, storeSet, replace } from './dom.js';
import { icon } from './icons.js';

export function button(label, { icon: ic, variant = 'secondary', size = '', onClick, type = 'button', title, attrs = {} } = {}) {
  const b = h('button', { type, class: `btn btn-${variant}${size ? ` btn-${size}` : ''}`, title, ...attrs },
    ic ? icon(ic, { size: size === 'sm' ? 16 : 18 }) : null, label ? h('span', {}, label) : null);
  if (onClick) b.addEventListener('click', onClick);
  return b;
}

export function iconButton(ic, label, onClick, { variant = 'ghost', pressed, attrs = {}, size = 18 } = {}) {
  const b = h('button', {
    type: 'button', class: `icon-btn icon-btn-${variant}`, 'aria-label': label, title: label,
    'aria-pressed': pressed === undefined ? undefined : String(Boolean(pressed)), ...attrs,
  }, icon(ic, { size }));
  if (onClick) b.addEventListener('click', onClick);
  return b;
}

export function linkButton(label, href, { icon: ic, variant = 'secondary', download, external, size = '', attrs = {} } = {}) {
  return h('a', {
    class: `btn btn-${variant}${size ? ` btn-${size}` : ''}`, href, download: download === undefined ? undefined : download,
    target: external ? '_blank' : undefined, rel: external ? 'noopener' : undefined, ...attrs,
  }, ic ? icon(ic, { size: size === 'sm' ? 16 : 18 }) : null, h('span', {}, label));
}

export function field(label, control, { hint, id, extra } = {}) {
  const cid = id || control.id || uid('f');
  control.id = cid;
  const hintEl = hint ? h('p', { class: 'field-hint', id: `${cid}-hint` }, hint) : null;
  if (hintEl) control.setAttribute('aria-describedby', `${cid}-hint`);
  return h('div', { class: 'field' },
    h('div', { class: 'field-row' }, h('label', { class: 'field-label', for: cid }, label), extra || null),
    control, hintEl);
}

export function textInput({ value = '', placeholder = '', maxLength, name, type = 'text', attrs = {} } = {}) {
  return h('input', { class: 'input', type, value, placeholder, maxlength: maxLength, name, autocomplete: 'off', ...attrs });
}

export function select(options, value, { onChange, attrs = {} } = {}) {
  const s = h('select', { class: 'input select', ...attrs },
    options.map(([v, label]) => h('option', { value: v }, label)));
  s.value = value ?? (options[0] ? options[0][0] : '');
  if (onChange) s.addEventListener('change', () => onChange(s.value));
  return s;
}

export function numberInput({ value, min, max, step = 1, placeholder = '', attrs = {} } = {}) {
  const i = h('input', { class: 'input', type: 'number', min, max, step, placeholder, inputmode: 'decimal', ...attrs });
  if (value !== undefined && value !== null) i.value = String(value);
  return i;
}

export function readNumber(input, { integer = false } = {}) {
  const raw = input.value.trim();
  if (raw === '') return null;
  const n = Number(raw);
  if (!Number.isFinite(n)) return null;
  return integer ? Math.round(n) : n;
}

// A labelled switch (checkbox with role=switch).
export function toggle(label, checked = false, { onChange, hint } = {}) {
  const id = uid('sw');
  const input = h('input', { type: 'checkbox', role: 'switch', id, class: 'switch-input', checked });
  if (onChange) input.addEventListener('change', () => onChange(input.checked));
  const el = h('div', { class: 'switch-field' },
    h('label', { class: 'switch', for: id }, input, h('span', { class: 'switch-track', 'aria-hidden': 'true' }),
      h('span', { class: 'switch-label' }, label)),
    hint ? h('p', { class: 'field-hint', id: `${id}-hint` }, hint) : null);
  if (hint) input.setAttribute('aria-describedby', `${id}-hint`);
  el.input = input;
  return el;
}

// Single- or multi-choice chips (buttons with aria-pressed).
export function chips(options, { value, multiple = false, onChange, label, cls = '' } = {}) {
  let current = multiple ? new Set(value || []) : value;
  const group = h('div', { class: `chips ${cls}`, role: 'group', 'aria-label': label });
  const render = () => {
    for (const b of group.children) {
      const on = multiple ? current.has(b.dataset.value) : b.dataset.value === String(current);
      b.setAttribute('aria-pressed', String(on));
    }
  };
  for (const opt of options) {
    const [v, text, extra] = Array.isArray(opt) ? opt : [opt, opt];
    const b = h('button', { type: 'button', class: 'chip', dataset: { value: String(v) } }, text, extra ? h('span', { class: 'chip-extra' }, extra) : null);
    b.addEventListener('click', () => {
      if (multiple) {
        if (current.has(String(v))) current.delete(String(v)); else current.add(String(v));
      } else {
        current = String(v);
      }
      render();
      if (onChange) onChange(multiple ? [...current] : current);
    });
    group.append(b);
  }
  render();
  group.getValue = () => (multiple ? [...current] : current);
  group.setValue = (v) => { current = multiple ? new Set(v || []) : String(v); render(); };
  return group;
}

// Accessible tabs (roving tabindex, arrow keys). items: [[id, label, iconName]]
export function tabs(items, active, onChange, { label = 'Modes', cls = '' } = {}) {
  const list = h('div', { class: `tabs ${cls}`, role: 'tablist', 'aria-label': label });
  const buttons = items.map(([id, text, ic]) => {
    const b = h('button', { type: 'button', role: 'tab', class: 'tab', id: `tab-${id}`, dataset: { tab: id } },
      ic ? icon(ic, { size: 16 }) : null, h('span', {}, text));
    b.addEventListener('click', () => select(id, true));
    return b;
  });
  list.append(...buttons);
  function select(id, notify) {
    for (const b of buttons) {
      const on = b.dataset.tab === id;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
    }
    if (notify && onChange) onChange(id);
  }
  list.addEventListener('keydown', (ev) => {
    const idx = buttons.indexOf(document.activeElement);
    if (idx < 0) return;
    let next = null;
    if (ev.key === 'ArrowRight') next = (idx + 1) % buttons.length;
    else if (ev.key === 'ArrowLeft') next = (idx - 1 + buttons.length) % buttons.length;
    else if (ev.key === 'Home') next = 0;
    else if (ev.key === 'End') next = buttons.length - 1;
    if (next === null) return;
    ev.preventDefault();
    buttons[next].focus();
    select(buttons[next].dataset.tab, true);
  });
  select(active, false);
  list.select = (id) => select(id, false);
  return list;
}

// Range slider with a live value readout.
export function slider({ label, min, max, step = 0.01, value, format = (v) => String(v), hint, onInput }) {
  const id = uid('rng');
  const out = h('output', { class: 'slider-value', for: id });
  const input = h('input', { type: 'range', class: 'range', id, min, max, step });
  input.value = String(value ?? min);
  const sync = () => {
    out.textContent = format(Number(input.value));
    const pct = ((Number(input.value) - Number(min)) / (Number(max) - Number(min) || 1)) * 100;
    input.style.setProperty('--fill', `${pct}%`);
    input.setAttribute('aria-valuetext', out.textContent);
  };
  input.addEventListener('input', () => { sync(); if (onInput) onInput(Number(input.value)); });
  sync();
  const el = field(label, input, { hint, id, extra: out });
  el.input = input;
  el.getValue = () => Number(input.value);
  el.setValue = (v) => { input.value = String(v); sync(); };
  return el;
}

// Seed with dice (new random) and lock (keep across generations).
export function seedField(storeKey, { label = 'Seed' } = {}) {
  const input = numberInput({ min: 0, max: 2147483647, step: 1, placeholder: 'Random', attrs: { 'aria-label': label } });
  let locked = Boolean(storeGet(`${storeKey}.seedLock`, false));
  const saved = storeGet(`${storeKey}.seed`, null);
  if (locked && saved !== null) input.value = String(saved);
  let useOnce = false;
  const dice = iconButton('dice', 'Roll a new random seed', () => { input.value = String(randomSeed()); useOnce = true; });
  const lock = iconButton(locked ? 'lock' : 'unlock', 'Lock seed', null, { pressed: locked });
  const setLock = (v) => {
    locked = v;
    storeSet(`${storeKey}.seedLock`, v);
    lock.setAttribute('aria-pressed', String(v));
    replace(lock, icon(v ? 'lock' : 'unlock', { size: 18 }));
    lock.classList.toggle('is-on', v);
  };
  lock.addEventListener('click', () => setLock(!locked));
  setLock(locked);
  const id = uid('seed');
  input.id = id;
  const el = h('div', { class: 'field' },
    h('div', { class: 'field-row' }, h('label', { class: 'field-label', for: id }, label)),
    h('div', { class: 'input-group' }, input, dice, lock),
    h('p', { class: 'field-hint' }, 'Unlocked: a fresh seed for every run. Locked: repeat this seed.'));
  // Value for the next run: locked keeps the field; unlocked rolls a new seed.
  // Typing a seed by hand locks it (the user clearly wants that seed).
  input.addEventListener('input', () => { if (input.value.trim() !== '' && !locked) setLock(true); });
  el.next = () => {
    let v = readNumber(input, { integer: true });
    const keep = (locked || useOnce) && v !== null;
    useOnce = false;
    if (!keep) {
      v = randomSeed();
      input.value = String(v);
    }
    storeSet(`${storeKey}.seed`, v);
    return v;
  };
  el.set = (v, lockIt = false) => {
    input.value = v === null || v === undefined ? '' : String(v);
    if (lockIt) setLock(true);
  };
  el.input = input;
  return el;
}

// Large prompt textarea with a character counter; Ctrl/Cmd+Enter submits.
export function composer({ label = 'Prompt', placeholder = '', maxLength = 4000, rows = 4, onSubmit, value = '', id }) {
  const cid = id || uid('prompt');
  const ta = h('textarea', { id: cid, class: 'input composer-input', rows, maxlength: maxLength, placeholder, spellcheck: 'true' });
  ta.value = value;
  const count = h('span', { class: 'composer-count', 'aria-live': 'off' });
  const sync = () => { count.textContent = `${ta.value.length} / ${maxLength}`; };
  ta.addEventListener('input', sync);
  ta.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      if (onSubmit) onSubmit();
    }
  });
  sync();
  const el = h('div', { class: 'composer' },
    h('label', { class: 'field-label', for: cid }, label),
    ta,
    h('div', { class: 'composer-foot' },
      h('span', { class: 'kbd-hint' }, h('kbd', {}, 'Ctrl'), '+', h('kbd', {}, 'Enter'), ' to generate'), count));
  el.textarea = ta;
  el.get = () => ta.value.trim();
  el.set = (v) => { ta.value = v || ''; sync(); };
  return el;
}

// Collapsible section (native <details>).
export function disclosure(title, body, { open = false, ic } = {}) {
  const d = h('details', { class: 'disclosure', open },
    h('summary', {}, ic ? icon(ic, { size: 16 }) : null, h('span', {}, title), icon('chevronDown', { size: 16, cls: 'disclosure-chev' })),
    h('div', { class: 'disclosure-body' }, body));
  return d;
}

export function progressBar(progress, label = 'Progress') {
  const determinate = typeof progress === 'number' && Number.isFinite(progress);
  const pct = determinate ? Math.round(Math.max(0, Math.min(1, progress)) * 100) : null;
  const bar = h('div', {
    class: `progress ${determinate ? '' : 'progress-indeterminate'}`, role: 'progressbar', 'aria-label': label,
    'aria-valuemin': determinate ? '0' : undefined, 'aria-valuemax': determinate ? '100' : undefined,
    'aria-valuenow': determinate ? String(pct) : undefined,
  }, h('div', { class: 'progress-fill' }));
  if (determinate) bar.firstChild.style.width = `${pct}%`;
  return bar;
}

export function skeletonGrid(n = 6, cls = 'grid-media') {
  return h('div', { class: `${cls} skeleton-wrap`, 'aria-hidden': 'true' },
    Array.from({ length: n }, () => h('div', { class: 'skeleton skeleton-tile' })));
}

export function skeletonLines(n = 3) {
  return h('div', { class: 'stack-sm', 'aria-hidden': 'true' },
    Array.from({ length: n }, (_, i) => h('div', { class: 'skeleton skeleton-line', style: { width: `${90 - i * 18}%` } })));
}

export function loading(text = 'Loading…') {
  return h('p', { class: 'loading', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), text);
}

export function emptyState({ icon: ic = 'sparkles', title, text, action }) {
  return h('div', { class: 'empty' },
    h('div', { class: 'empty-art', 'aria-hidden': 'true' }, icon(ic, { size: 28 })),
    h('p', { class: 'empty-title' }, title),
    text ? h('p', { class: 'empty-text' }, text) : null,
    action || null);
}

export function callout(tone, title, text, actions = []) {
  return h('div', { class: `callout callout-${tone}`, role: tone === 'danger' ? 'alert' : 'status' },
    icon(tone === 'danger' ? 'alert' : 'info', { size: 18, cls: 'callout-ic' }),
    h('div', { class: 'callout-body' },
      title ? h('p', { class: 'callout-title' }, title) : null,
      text ? h('div', { class: 'callout-text' }, text) : null,
      actions.length ? h('div', { class: 'row-wrap' }, actions) : null));
}

export function badge(text, tone = 'neutral') {
  return h('span', { class: `badge badge-${tone}` }, text);
}

export function card(title, body, { actions, cls = '', level = 2, sub } = {}) {
  const id = uid('card');
  return h('section', { class: `card ${cls}`, 'aria-labelledby': id },
    h('header', { class: 'card-head' },
      h('div', {}, h(`h${level}`, { class: 'card-title', id }, title), sub ? h('p', { class: 'card-sub' }, sub) : null),
      actions ? h('div', { class: 'card-actions' }, actions) : null),
    body);
}

// Poster / thumbnail for an asset. Video posters exist only when the backend
// probed the file (it then also wrote a thumbnail); otherwise a placeholder.
export function hasThumb(asset) {
  if (asset.type === 'image') return true;
  if (asset.type === 'video') return Boolean(asset.width || asset.frame_count);
  return false;
}

export function assetThumb(asset, { lazy = true } = {}) {
  if (hasThumb(asset)) {
    return h('img', {
      class: 'thumb-img', src: asset.thumbnail_url, alt: '', loading: lazy ? 'lazy' : undefined, decoding: 'async',
      draggable: 'false',
    });
  }
  const ic = asset.type === 'audio' ? 'music' : asset.type === 'video' ? 'film' : 'image';
  return h('div', { class: `thumb-ph thumb-ph-${asset.type}` }, icon(ic, { size: 28 }));
}

// Dropzone: drag & drop or click to pick; calls onFile(File).
export function dropzone({ accept, label, hint, onFile }) {
  const id = uid('file');
  const input = h('input', { type: 'file', id, accept, class: 'sr-only' });
  const zone = h('label', { class: 'dropzone', for: id },
    icon('upload', { size: 22 }),
    h('span', { class: 'dropzone-title' }, label),
    hint ? h('span', { class: 'dropzone-hint' }, hint) : null);
  const bar = h('div', { class: 'upload-progress', hidden: true });
  const el = h('div', { class: 'dropzone-wrap' }, input, zone, bar);
  input.addEventListener('change', () => {
    if (input.files && input.files[0]) onFile(input.files[0]);
    input.value = '';
  });
  for (const t of ['dragenter', 'dragover']) {
    zone.addEventListener(t, (ev) => { ev.preventDefault(); zone.classList.add('is-drag'); });
  }
  for (const t of ['dragleave', 'drop']) {
    zone.addEventListener(t, () => zone.classList.remove('is-drag'));
  }
  zone.addEventListener('drop', (ev) => {
    ev.preventDefault();
    const f = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
    if (f) onFile(f);
  });
  el.input = input;
  el.progress = (fraction, text) => {
    bar.hidden = fraction === null;
    if (fraction === null) return;
    replace(bar, progressBar(fraction, 'Upload progress'), h('span', { class: 'upload-text' }, text || `Uploading… ${Math.round(fraction * 100)}%`));
  };
  return el;
}

export function kv(rows) {
  const dl = h('dl', { class: 'kv' });
  for (const [k, v] of rows) {
    if (v === undefined || v === null || v === '') continue;
    dl.append(h('dt', {}, k), h('dd', {}, v));
  }
  return dl;
}

export function statusDot(tone) {
  return h('span', { class: `dot dot-${tone}`, 'aria-hidden': 'true' });
}

export function pageHeader(title, sub, actions) {
  return h('header', { class: 'page-head' },
    h('div', { class: 'page-head-text' }, h('h1', { class: 'page-title', tabindex: '-1' }, title), sub ? h('p', { class: 'page-sub' }, sub) : null),
    actions ? h('div', { class: 'page-actions' }, actions) : null);
}
