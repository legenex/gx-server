import { api } from '../api.js';
import { h, clear, errorBox, spinner } from '../dom.js';

let root;
let viewEl;
let metaEl;
let streams = [];
let current = null;
let autoRefresh = false;

const els = {};

async function load({ signal } = {}) {
  if (!current) return;
  const lines = els.lines.value;
  const q = els.query.value.trim();
  const params = new URLSearchParams({ lines });
  if (q) params.set('q', q);
  metaEl.textContent = 'Loading…';
  const data = await api.get(`/api/logs/${encodeURIComponent(current)}?${params}`, { signal });
  const atBottom = viewEl.scrollHeight - viewEl.scrollTop - viewEl.clientHeight < 40;
  clear(viewEl);
  if (data.error) viewEl.append(h('span', { class: 'text-warn' }, `[${data.error}]\n`));
  const needle = q.toLowerCase();
  for (const line of data.lines) {
    const row = h('span', { class: `log-line${/\b(ERROR|FATAL|CRIT|Traceback|FAIL)\b/.test(line) ? ' log-err' : (/\b(WARN|WARNING)\b/.test(line) ? ' log-warn' : '')}` });
    if (needle) {
      const lower = line.toLowerCase();
      let pos = 0;
      for (;;) {
        const i = lower.indexOf(needle, pos);
        if (i < 0) { row.append(line.slice(pos)); break; }
        row.append(line.slice(pos, i), h('mark', {}, line.slice(i, i + needle.length)));
        pos = i + needle.length;
      }
    } else {
      row.textContent = line;
    }
    viewEl.append(row, '\n');
  }
  if (atBottom || !autoRefresh) viewEl.scrollTop = viewEl.scrollHeight;
  metaEl.textContent = `${data.stream.label} · ${data.stream.node === 'node1' ? 'gx10-01' : 'gx10-02'} · ${data.source} · ${data.count} line(s) · ${data.ms} ms · credentials redacted`;
}

function select(id) {
  current = id;
  for (const b of root.querySelectorAll('.stream-list button')) {
    b.setAttribute('aria-pressed', String(b.dataset.id === id));
  }
  history.replaceState(null, '', `#/logs/${id}`);
  load().catch((err) => { clear(viewEl).append(errorBox(err)); });
}

export default {
  title: 'Logs',
  // Polled every 5 s, but refresh() only fetches when auto-refresh is ticked.
  interval: 5,
  async mount(el, { params }) {
    root = el;
    clear(root).append(spinner());
    const data = await api.get('/api/logs');
    streams = data.streams;
    const groups = {};
    for (const s of streams) (groups[s.group] = groups[s.group] || []).push(s);

    els.lines = h('select', { id: 'log-lines' },
      [50, 100, 200, 500, 1000, 2000].map((n) => h('option', { value: n, selected: n === data.limits.default }, `${n} lines`)));
    els.query = h('input', { id: 'log-query', type: 'search', placeholder: 'filter (case-insensitive)', maxlength: 200 });
    const refreshBtn = h('button', { class: 'btn btn-primary btn-sm', type: 'button' }, 'Refresh');
    const auto = h('input', { type: 'checkbox', id: 'log-auto' });
    const dl = h('button', { class: 'btn btn-ghost btn-sm', type: 'button' }, 'Download excerpt');
    refreshBtn.addEventListener('click', () => load().catch((err) => clear(viewEl).append(errorBox(err))));
    els.query.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') refreshBtn.click(); });
    els.lines.addEventListener('change', () => refreshBtn.click());
    auto.addEventListener('change', () => { autoRefresh = auto.checked; });
    dl.addEventListener('click', async () => {
      if (!current) return;
      const params2 = new URLSearchParams({ lines: els.lines.value, format: 'text' });
      if (els.query.value.trim()) params2.set('q', els.query.value.trim());
      const res = await fetch(`/api/logs/${encodeURIComponent(current)}?${params2}`, { credentials: 'same-origin' });
      const blob = await res.blob();
      const a = h('a', { href: URL.createObjectURL(blob), download: `gx-${current}.log` });
      document.body.append(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    });

    const list = h('nav', { class: 'stream-list', 'aria-label': 'Log streams' },
      Object.entries(groups).map(([g, items]) => h('div', { class: 'stream-group' },
        h('h2', { class: 'stream-group-title' }, g),
        items.map((s) => {
          const b = h('button', { type: 'button', 'data-id': s.id, 'aria-pressed': 'false' },
            s.label, h('span', { class: 'muted small' }, s.node === 'node1' ? ' · gx10-01' : ' · gx10-02'));
          b.addEventListener('click', () => select(s.id));
          return b;
        }))));
    viewEl = h('pre', { class: 'log-view', tabindex: '0', 'aria-label': 'Log output', 'aria-live': 'off' });
    metaEl = h('p', { class: 'muted small', role: 'status' }, 'Choose a stream.');
    clear(root).append(
      h('p', { class: 'lead' }, 'Predefined streams only — no file browsing. Every line is passed through the credential redactor before it reaches the browser.'),
      h('div', { class: 'logs-layout' },
        list,
        h('div', { class: 'logs-main' },
          h('div', { class: 'toolbar' },
            h('label', { for: 'log-lines', class: 'sr-only' }, 'Lines'), els.lines,
            h('label', { for: 'log-query', class: 'sr-only' }, 'Filter'), els.query,
            refreshBtn, h('label', { class: 'inline' }, auto, ' auto-refresh (5 s)'), dl),
          metaEl, viewEl)));
    const initial = params && params[0] && streams.some((s) => s.id === params[0]) ? params[0] : 'orchestrator';
    select(initial);
  },
  async refresh({ signal }) {
    if (autoRefresh) await load({ signal });
  },
  unmount() { autoRefresh = false; },
};
