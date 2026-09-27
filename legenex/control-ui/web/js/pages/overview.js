// OVERVIEW: the cluster at a glance. Every number comes from /api/overview,
// /api/models and /api/actions — the SSE stream (/api/stream) only speeds the
// queue + lifecycle badges up; polling remains the source of truth.
import { api, openStream } from '../api.js';
import {
  h, clear, card, kv, levelBadge, stateBadge, table, errorBox, spinner,
} from '../dom.js';
import { nodeCard, modelTile, gitBlock, servicesTable, lockLedger, operate } from './common.js';

let root;
let ctxRef;
let stream = null;
let busy = false;
let actions = {};
let outputEl;
let last = { models: null };

function quickActions(gx, queue) {
  const wrap = h('div', { class: 'btn-row' });
  const specs = ['gxmax_start', 'gxmax_restart', 'gxmax_stop', 'gxmax_drain']
    .map((n) => actions[n]).filter(Boolean);
  if (!specs.length) {
    wrap.append(h('span', { class: 'muted small' }, 'Lifecycle actions are not loaded.'));
    return wrap;
  }
  const profile = h('select', { id: 'ov-profile', 'aria-label': 'Serving profile for start / restart' },
    ['fast', 'balanced', 'swarm', 'deep', 'long'].map((p) => h('option', { value: p, selected: p === 'balanced' }, p)));
  wrap.append(h('label', { class: 'inline', for: 'ov-profile' }, 'profile ', profile));
  for (const spec of specs) {
    const needsProfile = (spec.args || []).includes('profile');
    const btn = h('button', {
      type: 'button', class: `btn btn-sm ${spec.danger === 'danger' ? 'btn-danger' : ''}`,
      disabled: busy, 'data-op': spec.name, title: spec.description,
    }, spec.label);
    btn.addEventListener('click', async () => {
      busy = true;
      for (const b of root.querySelectorAll('button[data-op]')) b.disabled = true;
      const args = needsProfile ? { profile: profile.value } : {};
      await operate(spec, { outputEl, args, onDone: () => { busy = false; ctxRef.refreshNow(); } });
      busy = false;
    });
    wrap.append(btn);
  }
  return wrap;
}

function queueLine(queue) {
  if (!queue || queue.available === false) {
    return `unavailable: ${queue && queue.reason ? queue.reason : 'scheduler did not answer'}`;
  }
  return `queued ${queue.queued ?? '—'} · active ${queue.active ?? '—'}`
    + ` · UI operations running ${(queue.ui_running || []).length}`;
}

function render(ov, models) {
  const gx = ov.gxmax || {};
  const grid = h('div', { class: 'grid grid-dash' });

  grid.append(h('section', { class: `card hero level-${ov.overall}`, 'aria-label': 'Cluster state' },
    h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, 'Cluster'), levelBadge(ov.overall)),
    kv([
      ['Model', models && (models.registry || {}).registry_ok === false
        ? 'unavailable: registry.json is not at schema 2'
        : 'DeepSeek V4.1 Flash EXL3 (single model)'],
      ['gx-max', h('span', { class: 'gxmax-inline' }, stateBadge(gx.state),
        gx.phase && gx.phase !== 'idle' ? ` phase ${gx.phase}` : '',
        gx.profile ? ` · profile ${gx.profile}` : '')],
      ['Profile', gx.profile || '—'],
      ['Queue', h('span', { class: 'queue-inline' }, queueLine(ov.queue))],
      ['RDMA (both rails)', ov.rdma_ok ? stateBadge('ok', 'ACTIVE') : stateBadge('error', 'DEGRADED')],
      ['Tailscale (management)', levelBadge((ov.tailscale || {}).level,
        (ov.tailscale || {}).level === 'ok' ? 'connected' : 'degraded')],
      ['Git sync', levelBadge((ov.git || {}).level, ov.git && ov.git.match ? 'all HEADs match' : 'out of sync')],
      ['Registry', ov.registry_ok ? stateBadge('ok', 'schema 2') : stateBadge('error', 'not schema 2')],
    ]),
    h('p', { class: 'muted small' }, 'Two independent 128 GB nodes, one model in two tensor-parallel ranks. '
      + 'gx-max takes over both nodes; it is never auto-started at boot.'),
    quickActions(gx, ov.queue)));

  const modelCards = (models && models.models) || [];
  if (modelCards.length) {
    grid.append(card('Model & aliases',
      h('div', { class: 'model-grid' }, modelCards.map(modelTile)),
      h('p', { class: 'muted small' }, 'gx-max never silently falls back to the stock pack; '
        + 'the uncensored pack is the production candidate.')));
  }

  for (const n of ov.nodes) grid.append(nodeCard(n));

  grid.append(card('ConnectX / RoCE fabric',
    table(['Rail', 'gx10-01', 'gx10-02', 'Peer TCP', 'State'], ov.rails.map((r) => [
      r.name,
      `${r.node1_ip || '—'}${r.netdev ? ` (${r.netdev})` : ''}`,
      `${r.node2_ip || '—'}${r.rdma ? ` (${r.rdma})` : ''}`,
      r.tcp_probe,
      levelBadge(r.level, r.ok ? 'up' : 'down'),
    ]), { caption: 'RoCE rails' }),
    h('p', { class: 'muted small' }, 'Tailscale carries management only; model and NCCL traffic use the rails alone.')));

  grid.append(card('Locks & residency ledger', lockLedger(ov)));

  grid.append(card('Git sync', gitBlock(ov.git || {})));

  grid.append(card('Services', servicesTable(ov.services)));

  grid.append(card('Recent events & warnings',
    (ov.problems || []).length
      ? h('ul', { class: 'problems' }, ov.problems.map((p) => h('li', {},
        levelBadge(p.level, p.level), ' ', h('strong', {}, p.source), ': ', p.message)))
      : h('p', { class: 'muted' }, 'No current warnings.')));

  clear(root).append(grid, outputEl);
}

export default {
  title: 'Overview',
  interval: 5,
  mount(el, { ctx }) {
    root = el;
    ctxRef = ctx;
    busy = false;
    outputEl = h('pre', { class: 'job-output', hidden: true, 'aria-live': 'polite', tabindex: '0' });
    root.append(spinner());
    // SSE is a live enhancement for the queue + lifecycle badges; polling
    // refresh() remains and covers everything when the stream is absent.
    stream = openStream({
      queue: () => ctxRef.updateOverall(),
      lifecycle: () => ctxRef.updateOverall(),
    });
  },
  unmount() {
    if (stream) stream.close();
    stream = null;
  },
  async refresh({ signal }) {
    try {
      const [ov, models, act] = await Promise.all([
        api.get('/api/overview', { signal }),
        api.get('/api/models', { signal }).catch(() => null),
        api.get('/api/actions', { signal }).then((a) => {
          actions = {};
          for (const s of a.actions || []) actions[s.name] = s;
        }).catch(() => {}),
      ]);
      last = { models };
      render(ov, models);
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      clear(root).append(errorBox(err));
      throw err;
    }
  },
};
