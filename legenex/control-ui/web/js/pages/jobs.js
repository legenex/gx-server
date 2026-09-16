import { api } from '../api.js';
import {
  h, clear, card, kv, stateBadge, table, errorBox, duration, clock,
} from '../dom.js';

let root;
let eventsEl;
let follow = true;

const ACQUIRE_STEPS = [
  ['queued', 'Queued'], ['preflight', 'Preflight'], ['draining', 'Draining'], ['admission', 'Admission'],
  ['loading_rank1', 'Loading rank 1'], ['loading_rank0', 'Loading rank 0'], ['warming', 'Warming / weights'],
  ['ready', 'Ready'], ['serving', 'Serving'],
];
const RELEASE_STEPS = [
  ['draining_requests', 'Draining requests'], ['stopping_ranks', 'Stopping ranks'],
  ['memory_recovery', 'Memory recovery'], ['restoring', 'Restoring workloads'], ['released', 'Released'],
];

function stepper(steps, current, reached, failed) {
  const idx = steps.findIndex(([k]) => k === current);
  return h('ol', { class: 'stepper' }, steps.map(([key, label], i) => {
    let cls = 'todo';
    if (reached.has(key) || (idx >= 0 && i < idx)) cls = 'done';
    if (key === current) cls = failed ? 'failed' : 'current';
    return h('li', { class: `step step-${cls}`, 'aria-current': key === current ? 'step' : null },
      h('span', { class: 'step-dot', 'aria-hidden': 'true' }), label);
  }));
}

function render(d) {
  const gx = d.gxmax || {};
  const job = d.gxmax_active_job;
  const last = (d.gxmax_history || [])[0];
  const showing = job || last;
  const reached = new Set(((showing && showing.phases) || []).map((p) => p.phase));
  const isRelease = showing && String(showing.kind).startsWith('release');
  let current = gx.phase;
  if (gx.state === 'acquiring' && gx.waiters && !reached.size) current = 'queued';

  const lifecycle = card('gx-max lifecycle',
    kv([
      ['State', stateBadge(gx.state)],
      ['Phase', `${gx.phase || '—'}${gx.phase_seconds ? ` (for ${duration(gx.phase_seconds)})` : ''}`],
      ['Waiting requests (queue)', String(gx.waiters ?? 0)],
      ['Last error', gx.last_error || 'none'],
    ]),
    h('h3', {}, isRelease ? 'Release' : 'Acquire'),
    stepper(isRelease ? RELEASE_STEPS : ACQUIRE_STEPS, current, reached, gx.phase === 'failed'),
    job ? kv([
      ['Active job', job.kind],
      ['Started', clock(job.started)],
      ['Elapsed', duration(job.elapsed_seconds)],
    ]) : h('p', { class: 'muted' }, 'No gx-max job is running.'));

  const history = card('gx-max job history',
    table(['Job', 'Started', 'Elapsed', 'Startup', 'Outcome', 'Phases', 'Error'],
      (d.gxmax_history || []).map((j) => [
        j.kind, clock(j.started), duration(j.elapsed_seconds),
        j.startup_seconds ? `${j.startup_seconds} s` : '—',
        stateBadge(j.outcome === 'ready' || j.outcome === 'released' ? 'succeeded' : (j.outcome === 'failed' ? 'failed' : 'warn'), j.outcome),
        (j.phases || []).map((p) => p.phase).join(' → '),
        j.error ? h('span', { class: 'text-crit small' }, j.error.slice(0, 300)) : '',
      ]), { caption: 'gx-max job history', empty: 'No gx-max job recorded since the orchestrator history was introduced.' }));

  const media = d.media || {};
  const mediaCard = card('Media queue (gx10-02)',
    media.status ? kv([
      ['Generation slot', media.busy ? stateBadge('running', `busy: ${media.held_by} (${media.held_for_seconds} s)`) : stateBadge('idle', 'free')],
      ['Videos waiting', String(media.video_queue_depth ?? 0)],
      ['ComfyUI queue', String((media.comfyui || {}).queue_depth ?? '—')],
    ]) : h('p', { class: 'muted' }, 'Media router not reachable.'));

  const uiJobs = card('Control-UI operations',
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
  clear(top).append(h('div', { class: 'grid grid-2' }, lifecycle, mediaCard), history, uiJobs);
}

export default {
  title: 'Jobs / Queue',
  interval: 3,
  mount(el) {
    root = el;
    clear(root);
    const followBox = h('input', { type: 'checkbox', id: 'follow-events', checked: true });
    followBox.addEventListener('change', () => { follow = followBox.checked; });
    eventsEl = h('pre', { class: 'log-view', tabindex: '0', 'aria-label': 'gx-max lifecycle output' });
    root.append(
      h('p', { class: 'lead' }, 'Real lifecycle data from the orchestrator (read-only /lifecycle/gx-max/events), the media router queue and this UI\'s own operations. Nothing here is simulated.'),
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
