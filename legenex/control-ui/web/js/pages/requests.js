// REQUESTS: the orchestrator scheduler — live queue, active generations and
// history. PRIVACY: the scheduler never stores prompt bodies; every field on
// this page is metadata (allow-listed server-side in requests_view.py).
import { api } from '../api.js';
import {
  h, clear, card, stateBadge, table, errorBox, spinner, duration, num, toast, ago,
} from '../dom.js';

let root;
let filters = { state: '', project: '', profile: '', range: '24h' };
const RANGES = [['1h', 3600], ['6h', 21600], ['24h', 86400], ['7d', 604800], ['all', 0]];

function sinceParam() {
  const found = RANGES.find(([k]) => k === filters.range);
  return found && found[1] ? Math.floor(Date.now() / 1000 - found[1]) : '';
}

function ms(v) {
  if (v === null || v === undefined) return '—';
  return v >= 10000 ? `${num(v / 1000, 1)} s` : `${Math.round(v)} ms`;
}

function waitOf(r) {
  const enq = Number(r.enqueue_ts) || 0;
  const start = Number(r.start_ts) || 0;
  if (!start || !enq) return r.state === 'queued' ? 'in queue' : '—';
  return duration(start - enq);
}

function tokensOf(r) {
  const p = r.prompt_tokens ?? null;
  const c = r.completion_tokens ?? null;
  if (p === null && c === null) return '—';
  return `${p ?? '—'} + ${c ?? '—'}${r.cached_tokens ? ` (${r.cached_tokens} cached)` : ''}`;
}

function recRows(records) {
  return records.map((r) => [
    h('code', {}, r.id || '—'),
    r.project || 'unknown',
    r.agent || '—',
    r.task || '—',
    r.priority ?? '—',
    r.profile || '—',
    r.reasoning || '—',
    stateBadge(r.state || 'unknown'),
    waitOf(r),
    ms(r.ttft_ms),
    r.tps ? num(r.tps, 1) : '—',
    tokensOf(r),
    r.error ? h('span', { class: 'text-crit small' }, String(r.error).slice(0, 200)) : '',
  ]);
}

const HEADERS = ['ID', 'Project', 'Agent', 'Task', 'Priority', 'Profile', 'Reasoning', 'State',
  'Wait', 'TTFT', 'Tok/s', 'Tokens', 'Error'];

function liveRecords(status) {
  // The orchestrator status body may carry the records under a few keys
  // (Worker A's contract); anything else degrades to counts only.
  if (!status || status.available === false) return null;
  for (const key of ['queue', 'requests', 'records']) {
    if (Array.isArray(status[key])) return status[key];
  }
  return null;
}

function liveCard(status) {
  if (!status || status.available === false) {
    return card('Queue now',
      h('p', { class: 'callout callout-warning' },
        `unavailable: ${status && status.reason ? status.reason : 'the orchestrator scheduler did not answer'}`));
  }
  const records = liveRecords(status);
  const counts = {};
  for (const key of ['queue', 'requests', 'records']) {
    if (Array.isArray(status[key])) {
      for (const r of status[key]) counts[r && r.state] = (counts[r && r.state] || 0) + 1;
    }
  }
  const body = [];
  if (status.checked_at) body.push(h('p', { class: 'muted small' }, `Snapshot from ${ago(status.checked_at)}.`));
  if (records && records.length) {
    body.push(table(HEADERS.slice(0, 8).concat(['Wait', 'Actions']), records.map((r) => {
      const row = recRows([r])[0];
      const row8 = row.slice(0, 10);
      const canCancel = ['queued', 'active'].includes(r.state);
      const btn = h('button', {
        type: 'button', class: 'btn btn-ghost btn-sm', disabled: !canCancel,
        title: canCancel ? 'Cancel this request (queued always; active best effort)' : 'Only queued/active requests can be cancelled',
        'data-cancel': r.id,
      }, 'Cancel');
      btn.addEventListener('click', () => cancel(r));
      return row8.concat([btn]);
    }), { caption: 'Live queue snapshot', empty: 'The queue is empty.' }));
  } else {
    const chips = Object.entries(counts).map(([k, v]) => h('span', { class: 'chip' }, `${k}: ${v}`));
    body.push(h('p', {}, chips.length ? chips : h('span', { class: 'muted' }, 'The queue is empty.')));
  }
  return card('Queue now', ...body);
}

async function cancel(r) {
  try {
    await api.post(`/api/requests/${encodeURIComponent(r.id)}/cancel`, {});
    toast(`Cancel sent for ${r.id}`, 'ok');
  } catch (err) {
    toast(`Cancel failed: ${err.message}`, 'crit');
  }
}

async function retry(r) {
  try {
    await api.post(`/api/requests/${encodeURIComponent(r.id)}/retry`, {});
    toast(`Retry sent for ${r.id}`, 'ok');
  } catch (err) {
    toast(`Retry failed: ${err.message}`, 'crit');
  }
}

function filterBar(onChange) {
  const stateSel = h('select', { id: 'req-state', 'aria-label': 'Filter by state' },
    h('option', { value: '' }, 'any state'),
    ['queued', 'active', 'done', 'error', 'cancelled', 'timeout'].map((s) => h('option', {
      value: s, selected: filters.state === s,
    }, s)));
  const project = h('input', { id: 'req-project', type: 'search', placeholder: 'project',
    value: filters.project, maxlength: 120, 'aria-label': 'Filter by project' });
  const profileSel = h('select', { id: 'req-profile', 'aria-label': 'Filter by profile' },
    h('option', { value: '' }, 'any profile'),
    ['fast', 'balanced', 'swarm', 'deep', 'long', 'custom'].map((p) => h('option', {
      value: p, selected: filters.profile === p,
    }, p)));
  const range = h('select', { id: 'req-range', 'aria-label': 'Time range' },
    RANGES.map(([k]) => h('option', { value: k, selected: filters.range === k }, k)));
  const apply = h('button', { type: 'button', class: 'btn btn-primary btn-sm' }, 'Apply');
  apply.addEventListener('click', () => {
    filters = { state: stateSel.value, project: project.value.trim(), profile: profileSel.value, range: range.value };
    onChange();
  });
  project.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') apply.click(); });
  return h('div', { class: 'toolbar' },
    stateSel, project, profileSel,
    h('label', { class: 'inline', for: 'req-range' }, 'range ', range), apply);
}

async function loadHistory({ signal } = {}) {
  const params = new URLSearchParams();
  if (filters.state) params.set('state', filters.state);
  if (filters.project) params.set('project', filters.project);
  if (filters.profile) params.set('profile', filters.profile);
  const since = sinceParam();
  if (since) params.set('since', String(since));
  params.set('limit', '200');
  return api.get(`/api/requests?${params}`, { signal });
}

export default {
  title: 'Requests',
  interval: 8,
  async mount(el) {
    root = el;
    clear(root).append(
      h('p', { class: 'lead' }, 'The request scheduler: priorities, per-project caps and history. '
        + 'Prompt bodies are never stored, relayed or displayed — every row is metadata only.'),
      h('div', { class: 'live-holder' }, spinner()),
      card('History',
        filterBar(() => this.refresh().catch(() => {})),
        h('p', { class: 'muted small hist-note' }, ''),
        h('div', { class: 'hist-holder' }, spinner())));
  },
  async refresh({ signal } = {}) {
    try {
      const data = await loadHistory({ signal });
      clear(root.querySelector('.live-holder')).append(liveCard(data.status));
      const hist = data.history || {};
      const holder = root.querySelector('.hist-holder');
      clear(holder);
      if (hist.available === false) {
        holder.append(h('p', { class: 'callout callout-warning' },
          `unavailable: ${hist.reason || 'the scheduler history is not reachable'}`));
        return;
      }
      const records = hist.records || [];
      const note = root.querySelector('.hist-note');
      note.textContent = `${hist.count ?? records.length} record(s)${hist.note ? ` — ${hist.note}` : ''}`;
      holder.append(table(HEADERS.concat(['Actions']), records.map((r) => {
        const row = recRows([r])[0];
        const canCancel = ['queued', 'active'].includes(r.state);
        const canRetry = ['error', 'cancelled', 'timeout'].includes(r.state);
        const actions = h('div', { class: 'btn-row' });
        if (canCancel) {
          const b = h('button', { type: 'button', class: 'btn btn-ghost btn-sm', 'data-cancel': r.id }, 'Cancel');
          b.addEventListener('click', () => cancel(r));
          actions.append(b);
        }
        if (canRetry) {
          const b = h('button', { type: 'button', class: 'btn btn-sm', 'data-retry': r.id }, 'Retry');
          b.addEventListener('click', () => retry(r));
          actions.append(b);
        }
        return row.concat([actions]);
      }), { caption: 'Request history', empty: 'No requests in this window.' }));
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      clear(root.querySelector('.hist-holder')).append(errorBox(err));
      throw err;
    }
  },
};
