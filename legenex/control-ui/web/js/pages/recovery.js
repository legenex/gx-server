// RECOVERY: incidents, restart attempts with backoff, memory events, the
// watchdog state file and the gx-max lifecycle timeline. All read-only; the
// watchdog writer is a separate process and this page only relays what it
// actually wrote — no incident is ever invented.
import { api } from '../api.js';
import {
  h, clear, card, kv, stateBadge, table, errorBox, spinner, clock, duration,
} from '../dom.js';

let root;

function incidentsCard(w) {
  if (!w || w.available === false) {
    return card('Watchdog incidents',
      h('p', { class: 'callout callout-warning' }, `unavailable: ${w ? w.reason : 'the watchdog state is not readable'}`),
      h('p', { class: 'muted small' }, w && w.path ? h('code', { class: 'small' }, w.path) : ''));
  }
  const backoff = w.backoff_state || {};
  return card('Watchdog incidents',
    kv([
      ['Incidents recorded', String((w.incidents || []).length)],
      ['Restart attempts', String((w.restart_attempts || []).length)],
      ['Backoff state', backoff.backoff_s !== null && backoff.backoff_s !== undefined
        ? duration(backoff.backoff_s) : 'none in progress'],
      ['Source', h('code', { class: 'small' }, w.path || '—')],
    ]),
    table(['When', 'Kind', 'Detail', 'Attempt', 'Backoff'],
      (w.incidents || []).map((r) => [
        clock(r.ts),
        r.kind || '—',
        h('span', { class: 'small' }, r.detail || (r.raw ? h('code', { class: 'small' }, r.raw) : '')),
        r.attempt ?? '—',
        r.backoff_s ? duration(r.backoff_s) : '—',
      ]), { caption: 'Watchdog incidents', empty: 'No incidents recorded.' }));
}

function memoryCard(m) {
  const rows = [];
  const journal = (m && m.journal) || [];
  const varlog = (m && m.varlog) || [];
  rows.push(['journalctl --user (OOM lines)', journal.length
    ? h('pre', { class: 'code small' }, journal.join('\n')) : 'none in the last 400 lines']);
  rows.push(['/var/log fallback', varlog.length
    ? h('pre', { class: 'code small' }, varlog.join('\n')) : 'none']);
  if (m && m.note) rows.push(['Note', m.note]);
  return card('Memory events (kernel / OOM)', kv(rows));
}

function render(d) {
  const ev = d.lifecycle_events || [];
  const history = d.lifecycle_history || [];
  const hostwatch = d.hostwatch_tail || [];
  const grid = h('div', { class: 'grid grid-2' },
    card('gx-max',
      kv([
        ['Lifecycle state', stateBadge(d.gxmax || 'unknown')],
        ['Events in window', String(ev.length)],
      ]),
      h('p', { class: 'muted small' },
        'Restart attempts are bounded with exponential backoff (watchdog-owned); '
        + 'the lifecycle itself is orchestrator-owned.')),
    incidentsCard(d.watchdog));
  clear(root).append(grid,
    card('Orchestrator lifecycle events timeline',
      ev.length
        ? h('ol', { class: 'timeline' }, ev.map((e) => h('li', {},
          h('span', { class: 'muted small' }, `${e.ts ? clock(e.ts) : ''}${e.state ? ` [${e.state}]` : ''}`),
          h('br'),
          e.line || '')))
        : h('p', { class: 'muted' }, 'No lifecycle events recorded yet — the timeline fills during '
          + 'acquire / release.'),
      history.length ? table(['Job', 'Started', 'Elapsed', 'Outcome', 'Phases'],
        history.map((j) => [j.kind || '—', clock(j.started), duration(j.elapsed_seconds || 0),
          stateBadge(j.outcome === 'ready' || j.outcome === 'released' ? 'succeeded'
            : (j.outcome === 'failed' ? 'failed' : 'unknown'), j.outcome || '—'),
          ((j.phases || []).map((p) => p.phase)).join(' → ') || '—']),
        { caption: 'Lifecycle job history' }) : null),
    h('div', { class: 'grid grid-2' },
      card('hostwatch tail (gx10-01)',
        hostwatch.length ? h('pre', { class: 'code small' }, hostwatch.join('\n'))
          : h('p', { class: 'muted' }, 'No hostwatch lines available.'),
        h('p', { class: 'muted small' }, 'Lines are redacted server-side before reaching this page.')),
      memoryCard(d.memory_events)));
}

export default {
  title: 'Recovery',
  interval: 15,
  async mount(el) {
    root = el;
    clear(root).append(
      h('p', { class: 'lead' }, 'Incidents, bounded restart attempts with backoff, memory events and the '
        + 'gx-max lifecycle timeline. Everything here is read from real state files and logs; when a '
        + 'source does not exist yet, the page says so.'),
      spinner());
    render(await api.get('/api/recovery'));
  },
  async refresh({ signal }) {
    try {
      render(await api.get('/api/recovery', { signal }));
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      clear(root).append(errorBox(err));
      throw err;
    }
  },
};
