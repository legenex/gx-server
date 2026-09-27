import { api } from '../api.js';
import {
  h, clear, card, kv, stateBadge, table, errorBox, duration, clock,
} from '../dom.js';

let root;
let eventsEl;
let follow = true;

// Lifecycle phases come from the orchestrator's own event stream; the page
// renders whatever actually happened instead of a hardcoded step list.
function phaseTrack(job) {
  const phases = (job && job.phases) || [];
  if (!phases.length) return null;
  return h('ol', { class: 'phase-track', 'aria-label': 'Lifecycle phases' },
    phases.map((p) => h('li', { class: `phase ${p.phase === 'ready' || p.phase === 'serving' || p.phase === 'released' ? 'done' : 'active'}` },
      p.phase)));
}

function render(d) {
  const gx = d.gxmax || {};
  const job = d.gxmax_active_job;
  const last = (d.gxmax_history || [])[0];

  const lifecycle = card('gx-max lifecycle',
    kv([
      ['State', stateBadge(gx.state)],
      ['Phase', `${gx.phase || '—'}${gx.phase_seconds ? ` (for ${duration(gx.phase_seconds)})` : ''}`],
      ['Profile', gx.profile || '—'],
      ['Waiting requests (queue)', String(gx.waiters ?? 0)],
      ['Last error', gx.last_error || 'none'],
    ]),
    job ? kv([
      ['Active job', job.kind],
      ['Started', clock(job.started)],
      ['Elapsed', duration(job.elapsed_seconds)],
    ]) : h('p', { class: 'muted' }, 'No gx-max job is running.'),
    phaseTrack(job || last));

  const queue = d.queue || {};
  lifecycle.prepend(h('p', { class: 'muted small' }, queue.available === false
    ? `Scheduler queue: unavailable: ${queue.reason || 'orchestrator did not answer'}`
    : `Scheduler queue: queued ${queue.queued ?? '—'} · active ${queue.active ?? '—'}`));

  const history = card('gx-max job history',
    table(['Job', 'Started', 'Elapsed', 'Outcome', 'Phases', 'Error'],
      (d.gxmax_history || []).map((j) => [
        j.kind, clock(j.started), duration(j.elapsed_seconds),
        stateBadge(j.outcome === 'ready' || j.outcome === 'released' ? 'succeeded'
          : (j.outcome === 'failed' ? 'failed' : 'unknown'), j.outcome),
        ((j.phases || []).map((p) => p.phase)).join(' → '),
        j.error ? h('span', { class: 'text-crit small' }, j.error.slice(0, 300)) : '',
      ]), { caption: 'gx-max job history', empty: 'No gx-max job recorded yet.' }));

  const uiJobs = card('Control-UI operations (ActionRunner)',
    h('p', { class: 'muted small' }, 'Every operation is audited to /srv/logs/gx-control-ui/audit.log.'),
    table(['Operation', 'User', 'Started', 'Elapsed', 'State', ''], (d.ui_jobs || []).map((j) => {
      const btn = h('button', { class: 'btn btn-ghost btn-sm', type: 'button' }, 'Output');
      btn.addEventListener('click', async () => {
        const full = await api.get(`/api/actions/jobs/${j.id}`);
        const pre = h('pre', { class: 'job-output', tabindex: '0' }, (full.output || []).join('\n') || '(no output)');
        btn.replaceWith(pre);
      });
      return [j.label, j.user, clock(j.started), duration(j.elapsed_seconds), stateBadge(j.state), btn];
    }), { caption: 'Control UI operations', empty: 'No operations since the UI started.' }));

  const lines = (d.gxmax_events || []).map((e) => `${new Date(e.ts * 1000).toLocaleTimeString()} [${e.source}] ${e.line}`);
  eventsEl.textContent = lines.join('\n') || '(no lifecycle output yet — it appears here live during acquire/release)';
  if (follow) eventsEl.scrollTop = eventsEl.scrollHeight;

  const top = root.querySelector('.jobs-top');
  clear(top).append(h('div', { class: 'grid grid-2' }, lifecycle, uiJobs), history);
}

export default {
  title: 'Jobs / Actions',
  interval: 3,
  mount(el) {
    root = el;
    clear(root);
    const followBox = h('input', { type: 'checkbox', id: 'follow-events', checked: true });
    followBox.addEventListener('change', () => { follow = followBox.checked; });
    eventsEl = h('pre', { class: 'log-view', tabindex: '0', 'aria-label': 'gx-max lifecycle output' });
    root.append(
      h('p', { class: 'lead' }, 'The ActionRunner jobs and the gx-max lifecycle. Lifecycle changes go only '
        + 'through the orchestrator; orchestrator events are relayed here live, not simulated.'),
      h('div', { class: 'jobs-top' }),
      card('gx-max lifecycle output (live)',
        h('label', { class: 'inline' }, followBox, ' follow new lines'), eventsEl),
    );
  },
  async refresh({ signal }) {
    try {
      render(await api.get('/api/jobs', { signal }));
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      clear(root.querySelector('.jobs-top')).append(errorBox(err));
      throw err;
    }
  },
};
