// DOM and formatting helpers. Every element is built with createElement and
// textContent: no string is ever parsed as HTML anywhere in the Playground.

const SVG_NS = 'http://www.w3.org/2000/svg';

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  append(el, children);
  return el;
}

export function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v !== undefined && v !== null && v !== false) el.setAttribute(k, String(v));
  }
  for (const c of children.flat()) if (c) el.append(c);
  return el;
}

function setAttrs(el, attrs) {
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') el.className = Array.isArray(value) ? value.filter(Boolean).join(' ') : value;
    else if (key === 'text') el.textContent = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
    else if (key === 'value' && 'value' in el) el.value = value;
    else if (key === 'checked') el.checked = Boolean(value);
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, String(value));
  }
}

export function append(el, children) {
  for (const child of [children].flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  while (el && el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function replace(el, ...children) {
  clear(el);
  return append(el, children);
}

export const byId = (id) => document.getElementById(id);

let uidCounter = 0;
export function uid(prefix = 'u') {
  uidCounter += 1;
  return `${prefix}-${uidCounter}`;
}

export function debounce(fn, ms = 300) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

export function reducedMotion() {
  if (document.documentElement.dataset.motion === 'reduce') return true;
  return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

// ------------------------------------------------------------ storage
export function storeGet(key, fallback = null) {
  try {
    const v = localStorage.getItem(`gxpg.${key}`);
    return v === null ? fallback : JSON.parse(v);
  } catch {
    return fallback;
  }
}

export function storeSet(key, value) {
  try { localStorage.setItem(`gxpg.${key}`, JSON.stringify(value)); } catch { /* private mode */ }
}

// ------------------------------------------------------------ format
export function bytes(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = Number(n);
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(i && v < 10 ? 1 : 0)} ${units[i]}`;
}

export function elapsed(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return '0:00';
  let s = Math.max(0, Math.floor(Number(seconds)));
  const hh = Math.floor(s / 3600); s -= hh * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  const pad = (x) => String(x).padStart(2, '0');
  return hh ? `${hh}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export function mmss(seconds) {
  if (!Number.isFinite(Number(seconds))) return '0:00';
  const s = Math.max(0, Number(seconds));
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
}

export function ago(epoch) {
  if (!epoch) return '';
  const d = Math.max(0, Date.now() / 1000 - Number(epoch));
  if (d < 45) return 'just now';
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  if (d < 86400 * 14) return `${Math.round(d / 86400)} d ago`;
  return new Date(Number(epoch) * 1000).toLocaleDateString();
}

export function dateTime(epoch) {
  if (!epoch) return '—';
  return new Date(Number(epoch) * 1000).toLocaleString();
}

export function plural(n, word, many = `${word}s`) {
  return `${n} ${Number(n) === 1 ? word : many}`;
}

export function titleOf(asset) {
  if (!asset) return 'Untitled';
  return asset.title || (asset.prompt ? truncate(asset.prompt, 60) : '') || 'Untitled';
}

export function truncate(text, n) {
  const s = String(text || '');
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

export function randomSeed() {
  const buf = new Uint32Array(1);
  crypto.getRandomValues(buf);
  return buf[0] % 2147483647;
}

// ------------------------------------------------------------ toasts
export function toast(message, tone = 'ok', ms = 4500) {
  const box = byId('toasts');
  if (!box) return;
  const t = h('div', { class: `toast toast-${tone}` },
    h('span', { class: 'toast-dot', 'aria-hidden': 'true' }),
    h('span', { class: 'toast-msg' }, message));
  box.append(t);
  while (box.children.length > 4) box.firstChild.remove();
  setTimeout(() => {
    t.classList.add('toast-out');
    setTimeout(() => t.remove(), 250);
  }, ms);
}

// ------------------------------------------------------------ dialogs
// A <dialog> built on demand, removed from the DOM when it closes.
export function openDialog({ title, body, actions = [], className = '', label, onClose, initialFocus }) {
  const titleId = uid('dlg-title');
  const dlg = h('dialog', { class: `dialog ${className}`, 'aria-labelledby': title ? titleId : undefined,
    'aria-label': title ? undefined : (label || 'Dialog') });
  const closeBtn = h('button', { type: 'button', class: 'icon-btn dialog-x', 'aria-label': 'Close dialog' }, '×');
  closeBtn.addEventListener('click', () => dlg.close('cancel'));
  const head = title ? h('header', { class: 'dialog-head' }, h('h2', { id: titleId, class: 'dialog-title' }, title), closeBtn) : null;
  const content = h('div', { class: 'dialog-body' }, body);
  const foot = actions.length ? h('footer', { class: 'dialog-foot' }, actions) : null;
  dlg.append(...[head, content, foot].filter(Boolean));
  if (!title) dlg.append(closeBtn);
  const previous = document.activeElement;
  dlg.addEventListener('close', () => {
    if (onClose) onClose(dlg.returnValue);
    dlg.remove();
    if (previous && previous.isConnected && typeof previous.focus === 'function') previous.focus();
  });
  dlg.addEventListener('click', (ev) => {
    if (ev.target === dlg) dlg.close('cancel');
  });
  document.body.append(dlg);
  dlg.showModal();
  if (initialFocus) initialFocus.focus();
  return dlg;
}

export function confirmDialog({ title, message, okLabel = 'Confirm', danger = false, phrase = null, details = null }) {
  return new Promise((resolve) => {
    let input = null;
    const ok = h('button', { type: 'button', class: `btn ${danger ? 'btn-danger' : 'btn-primary'}`, 'data-role': 'confirm-ok' }, okLabel);
    const cancel = h('button', { type: 'button', class: 'btn btn-ghost' }, 'Cancel');
    const body = [typeof message === 'string' ? h('p', { class: 'dialog-text' }, message) : message, details];
    if (phrase) {
      const id = uid('phrase');
      input = h('input', { id, class: 'input', autocomplete: 'off', spellcheck: 'false', 'aria-describedby': `${id}-hint` });
      body.push(h('div', { class: 'field' },
        h('label', { for: id, class: 'field-label' }, 'Type the confirmation phrase'),
        input,
        h('p', { id: `${id}-hint`, class: 'field-hint' }, 'Type ', h('code', {}, phrase), ' to continue.')));
      ok.disabled = true;
      input.addEventListener('input', () => { ok.disabled = input.value.trim() !== phrase; });
      input.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' && !ok.disabled) ok.click(); });
    }
    let result = false;
    const dlg = openDialog({
      title, body, actions: [cancel, ok], className: 'dialog-confirm',
      onClose: () => resolve(result ? (phrase ? input.value.trim() : true) : false),
    });
    ok.addEventListener('click', () => { result = true; dlg.close('ok'); });
    cancel.addEventListener('click', () => dlg.close('cancel'));
    (input || cancel).focus();
  });
}

export function promptDialog({ title, label, value = '', okLabel = 'Save', maxLength = 200 }) {
  return new Promise((resolve) => {
    const id = uid('prompt');
    const input = h('input', { id, class: 'input', value, maxlength: String(maxLength), autocomplete: 'off' });
    const ok = h('button', { type: 'button', class: 'btn btn-primary' }, okLabel);
    const cancel = h('button', { type: 'button', class: 'btn btn-ghost' }, 'Cancel');
    let result = null;
    const form = h('form', { class: 'stack' },
      h('div', { class: 'field' }, h('label', { for: id, class: 'field-label' }, label), input));
    const dlg = openDialog({ title, body: form, actions: [cancel, ok], onClose: () => resolve(result) });
    ok.addEventListener('click', (ev) => { ev.preventDefault(); result = input.value.trim(); dlg.close('ok'); });
    form.addEventListener('submit', (ev) => { ev.preventDefault(); result = input.value.trim(); dlg.close('ok'); });
    cancel.addEventListener('click', () => dlg.close('cancel'));
    input.focus();
    input.select();
  });
}

// A side drawer (modal dialog anchored to the right edge).
export function openDrawer({ title, body, onClose, wide = false }) {
  return openDialog({ title, body, className: `drawer${wide ? ' drawer-wide' : ''}`, onClose });
}
