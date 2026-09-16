// DOM helpers. Everything is built with createElement/textContent; no
// untrusted string is ever parsed as HTML. The one exception is
// setTrustedHTML(), used only for documentation rendered by our own
// escape-first server renderer.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
    else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, String(value));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function setTrustedHTML(el, html) {
  // Only for server-rendered documentation (escape-first renderer).
  el.innerHTML = html; // eslint-disable-line no-unsanitized/property
}

const LEVEL_TEXT = { ok: 'Healthy', warn: 'Degraded', crit: 'Critical', unknown: 'Unknown' };

export function levelBadge(level, text) {
  const lvl = ['ok', 'warn', 'crit'].includes(level) ? level : 'unknown';
  return h('span', { class: `badge badge-${lvl}` },
    h('span', { class: 'dot', 'aria-hidden': 'true' }), text || LEVEL_TEXT[lvl]);
}

const STATE_LEVEL = {
  loaded: 'ok', ready: 'ok', serving: 'ok', succeeded: 'ok', active: 'ok', free: 'ok', running: 'ok',
  loading: 'warn', unloading: 'warn', acquiring: 'warn', releasing: 'warn', queued: 'warn', held: 'warn',
  unloaded: 'idle', down: 'idle', stopped: 'idle', idle: 'idle', inactive: 'idle', absent: 'idle', exited: 'idle',
  unavailable: 'crit', error: 'crit', failed: 'crit', dead: 'crit',
};

export function stateBadge(state, label) {
  const s = String(state || 'unknown').toLowerCase();
  const lvl = STATE_LEVEL[s] || 'unknown';
  return h('span', { class: `badge badge-${lvl}` },
    h('span', { class: 'dot', 'aria-hidden': 'true' }), label || s);
}

export function gib(bytes, digits = 1) {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return '—';
  return `${(bytes / 2 ** 30).toFixed(digits)} GiB`;
}

export function bytes(n) {
  if (n === null || n === undefined) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let i = 0;
  let v = Number(n);
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
}

export function num(n, digits = 1) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
  return Number(n).toFixed(digits);
}

export function duration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  let s = Math.max(0, Math.round(seconds));
  const d = Math.floor(s / 86400); s -= d * 86400;
  const hh = Math.floor(s / 3600); s -= hh * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  if (d) return `${d}d ${hh}h`;
  if (hh) return `${hh}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

export function ago(epoch) {
  if (!epoch) return '—';
  return `${duration(Date.now() / 1000 - epoch)} ago`;
}

export function clock(epoch) {
  if (!epoch) return '—';
  return new Date(epoch * 1000).toLocaleString();
}

export function short(sha, n = 10) {
  return sha ? String(sha).slice(0, n) : '—';
}

export function kv(rows) {
  const dl = h('dl', { class: 'kv' });
  for (const [k, v] of rows) {
    if (v === undefined) continue;
    dl.append(h('dt', {}, k), h('dd', {}, v === null || v === '' ? '—' : v));
  }
  return dl;
}

export function table(headers, rows, opts = {}) {
  const t = h('table', { class: `table ${opts.class || ''}` });
  if (opts.caption) t.append(h('caption', { class: 'sr-only' }, opts.caption));
  t.append(h('thead', {}, h('tr', {}, headers.map((x) => h('th', { scope: 'col' }, x)))));
  const tb = h('tbody');
  if (!rows.length) {
    tb.append(h('tr', {}, h('td', { colspan: headers.length, class: 'muted' }, opts.empty || 'Nothing to show.')));
  }
  for (const row of rows) tb.append(h('tr', {}, row.map((c) => h('td', {}, c))));
  t.append(tb);
  return h('div', { class: 'table-wrap', tabindex: '0', role: 'region', 'aria-label': opts.caption || 'table' }, t);
}

export function card(title, ...body) {
  const id = `c-${Math.random().toString(36).slice(2, 9)}`;
  return h('section', { class: 'card', 'aria-labelledby': id },
    h('h2', { class: 'card-title', id }, title), ...body);
}

export function meter(value, max, { label, warnAt, critAt, invert } = {}) {
  const pct = max ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  let lvl = 'ok';
  const test = invert ? 100 - pct : pct;
  if (critAt !== undefined && test >= critAt) lvl = 'crit';
  else if (warnAt !== undefined && test >= warnAt) lvl = 'warn';
  return h('div', {
    class: `meter meter-${lvl}`, role: 'meter', 'aria-valuemin': '0', 'aria-valuemax': String(max),
    'aria-valuenow': String(value), 'aria-label': label || 'usage',
  }, h('div', { class: 'meter-fill', style: { width: `${pct.toFixed(1)}%` } }));
}

export function copyButton(getText, label = 'Copy') {
  const btn = h('button', { class: 'btn btn-ghost btn-sm copy-btn', type: 'button' }, label);
  btn.addEventListener('click', async () => {
    const text = typeof getText === 'function' ? getText() : getText;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = h('textarea', { class: 'sr-only' });
      ta.value = text;
      document.body.append(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    btn.textContent = 'Copied';
    setTimeout(() => { btn.textContent = label; }, 1500);
  });
  return btn;
}

export function codeBlock(text, lang = '') {
  const pre = h('pre', { class: 'code' }, h('code', { class: lang ? `lang-${lang}` : '' }, text));
  return h('div', { class: 'code-wrap' }, copyButton(() => text), pre);
}

export function toast(message, level = 'ok', ms = 5000) {
  const box = document.getElementById('toasts');
  const t = h('div', { class: `toast toast-${level}` }, message);
  box.append(t);
  setTimeout(() => t.remove(), ms);
}

export function spinner(text = 'Loading…') {
  return h('p', { class: 'loading', role: 'status' }, h('span', { class: 'spin', 'aria-hidden': 'true' }), text);
}

export function errorBox(err) {
  return h('div', { class: 'callout callout-danger', role: 'alert' },
    h('strong', {}, 'Could not load: '), err && err.message ? err.message : String(err));
}

// Confirmation dialog; resolves to {ok, phrase}.
export function confirmDialog({ title, body, phrase, okLabel = 'Confirm', danger = true }) {
  const dlg = document.getElementById('confirm-dialog');
  document.getElementById('confirm-title').textContent = title;
  const bodyEl = clear(document.getElementById('confirm-body'));
  append(bodyEl, [typeof body === 'string' ? h('p', {}, body) : body]);
  const wrap = document.getElementById('confirm-phrase-wrap');
  const input = document.getElementById('confirm-phrase');
  const ok = document.getElementById('confirm-ok');
  ok.textContent = okLabel;
  ok.className = `btn ${danger ? 'btn-danger' : 'btn-primary'}`;
  input.value = '';
  wrap.hidden = !phrase;
  if (phrase) {
    document.getElementById('confirm-phrase-hint').textContent = phrase;
    ok.disabled = true;
    input.oninput = () => { ok.disabled = input.value !== phrase; };
  } else {
    ok.disabled = false;
    input.oninput = null;
  }
  return new Promise((resolve) => {
    dlg.onclose = () => resolve({ ok: dlg.returnValue === 'ok', phrase: input.value });
    dlg.returnValue = '';
    dlg.showModal();
    (phrase ? input : document.getElementById('confirm-cancel')).focus();
  });
}
