import { api } from '../api.js';
import {
  h, clear, card, kv, stateBadge, levelBadge, table, errorBox, clock, duration, codeBlock,
} from '../dom.js';
import { gitBlock, unitBadge, operate } from './common.js';

let root;
let outputEl;
let busy = false;
let ctxRef;

function actionButtons(actions) {
  const groups = { system: 'Safe checks & maintenance', infra: 'Restart non-model infrastructure' };
  return Object.entries(groups).map(([prefix, label]) => h('div', { class: 'action-group' },
    h('h3', {}, label),
    h('div', { class: 'action-list' }, actions.filter((a) => a.name.startsWith(`${prefix}.`)).map((a) => {
      const btn = h('button', {
        type: 'button', class: `btn btn-sm ${a.danger === 'safe' ? 'btn-primary' : ''}`, disabled: busy,
        'data-action': a.name,
      }, a.label);
      btn.addEventListener('click', async () => {
        busy = true;
        for (const b of root.querySelectorAll('button[data-action]')) b.disabled = true;
        await operate(a, { outputEl, onDone: () => { busy = false; ctxRef.refreshNow(); } });
        busy = false;
      });
      return h('div', { class: 'action-item' }, btn, h('p', { class: 'muted small' }, a.description));
    }))));
}

function render(d) {
  const units = [];
  for (const [node, list] of Object.entries(d.units)) {
    for (const u of list) units.push([node, h('code', {}, u.unit), unitBadge(u), u.enabled || '—']);
  }
  const top = root.querySelector('.settings-body');
  clear(top).append(
    h('div', { class: 'grid grid-2' },
      card('Project',
        kv([
          ['Project version', d.project_version],
          ['Control UI version', d.ui_version],
          ['Repository', h('code', {}, d.repo.remote)],
          ['Checkout (gx10-01)', h('code', {}, d.repo.path)],
          ['Branch', d.repo.branch || '—'],
          ['Kernel pin', h('code', {}, d.kernel_pin)],
          ['Running kernels', Object.entries(d.kernels).map(([n, k]) => `${n}: ${k || '?'}${k === d.kernel_pin ? ' ✓' : ' ✗'}`).join(' · ')],
          ['Active UI sessions', String(d.sessions)],
        ])),
      card('GitHub sync', gitBlock(d.git),
        kv([
          ['gx10-01 watcher', unitBadge(d.git.units.node1_watcher)],
          ['gx10-01 1-min fallback', unitBadge(d.git.units.node1_timer)],
          ['gx10-01 daily audit', unitBadge(d.git.units.node1_daily_audit)],
          ['gx10-02 reconcile timer', unitBadge(d.git.units.node2_reconcile)],
          ['gx10-02 daily audit', unitBadge(d.git.units.node2_daily_audit)],
        ]))),
    card('Actions',
      h('p', { class: 'muted' }, 'Only these fixed operations exist. There is no shell, no upgrade and no kernel/firmware button. Each run is audited.'),
      ...actionButtons(d.actions)),
    card('Configured endpoints',
      table(['Endpoint', 'URL', 'Reachable from'], d.endpoints.map((e) => [e.name, h('code', {}, e.url), e.scope]), { caption: 'Endpoints' })),
    h('div', { class: 'grid grid-2' },
      card('User services & timers',
        table(['Node', 'Unit', 'State', 'Enabled'], units, { caption: 'Units' }),
        table(['Timer', 'Next', 'Last'], d.timers.map((t) => [t.unit, t.next ? clock(t.next / 1e6) : '—', t.last ? clock(t.last / 1e6) : '—']), { caption: 'Timers on gx10-01' })),
      card('Runtime directories',
        table(['Path', 'Purpose'], d.runtime_dirs.map((r) => [h('code', {}, r.path), r.purpose]), { caption: 'Runtime directories' }))),
    h('div', { class: 'grid grid-2' },
      card('Credential hygiene (values never shown)',
        table(['Variable', 'State'], d.secrets.map((s) => [h('code', {}, s.name),
          s.state === 'set' ? stateBadge('ok', 'set') : (s.name === 'GX_ORCHESTRATOR_API_KEY' && s.state === 'placeholder'
            ? stateBadge('idle', 'not used (orchestrator is loopback-only)') : levelBadge('warn', s.state))]), { caption: 'Credential hygiene' })),
      card('Control UI password',
        h('p', {}, 'The password is set from a terminal on gx10-01, never from the browser. Changing it signs out every session.'),
        codeBlock('cd ~/Documents/Projects/Server/gx-cluster\nlegenex/control-ui/scripts/gx-ui-passwd            # prompt for a new password\nlegenex/control-ui/scripts/gx-ui-passwd --status   # show whether one is set', 'bash'))),
    card('Recent operations',
      table(['Operation', 'User', 'Started', 'Elapsed', 'State'], d.jobs.map((j) => [j.label, j.user, clock(j.started), duration(j.elapsed_seconds), stateBadge(j.state)]),
        { caption: 'Recent operations', empty: 'No operations yet.' })),
  );
}

export default {
  title: 'Settings / System',
  interval: 10,
  mount(el, { ctx }) {
    root = el;
    ctxRef = ctx;
    outputEl = h('pre', { class: 'job-output', hidden: true, 'aria-live': 'polite', tabindex: '0' });
    clear(root).append(outputEl, h('div', { class: 'settings-body' }));
  },
  async refresh({ signal }) {
    if (busy) return;
    try {
      render(await api.get('/api/system', { signal }));
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      clear(root.querySelector('.settings-body')).append(errorBox(err));
      throw err;
    }
  },
};
