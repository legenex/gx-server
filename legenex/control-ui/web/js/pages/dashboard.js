import { api } from '../api.js';
import {
  h, clear, card, kv, levelBadge, stateBadge, duration, table, bytes, errorBox,
} from '../dom.js';
import { nodeCard, modelTile, gitBlock, unitBadge, servicesTable, lockLedger } from './common.js';

let root;

function render(ov) {
  const gx = ov.gxmax || {};
  const grid = h('div', { class: 'grid grid-dash' });

  grid.append(h('section', { class: `card hero level-${ov.overall}`, 'aria-label': 'Cluster summary' },
    h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, 'Cluster'), levelBadge(ov.overall)),
    h('p', {}, 'Two independent 128 GB unified-memory nodes joined by two ConnectX-7 RoCE rails. ',
      'Memory is budgeted per node — there is no shared 256 GB pool.'),
    kv([
      ['Loaded aliases', ov.loaded_aliases.length ? ov.loaded_aliases.join(', ') : 'none'],
      ['gx-max', h('span', {}, stateBadge(gx.state), gx.phase && gx.phase !== 'idle' ? ` phase: ${gx.phase}` : '')],
      ['RDMA (both rails)', ov.rdma_ok ? stateBadge('ok', 'ACTIVE') : stateBadge('error', 'DEGRADED')],
      ['Tailscale (management)', levelBadge(ov.tailscale.level, ov.tailscale.level === 'ok' ? 'connected' : 'degraded')],
      ['Git sync', levelBadge(ov.git.level, ov.git.match ? 'all HEADs match' : 'out of sync')],
      ['Queue', `gx-max waiters ${ov.queue.gxmax_waiters ?? 0} · media ${ov.queue.media_busy ? 'busy' : 'idle'} (${ov.queue.media_video_queue ?? 0} queued) · UI operations running ${ov.queue.ui_running.length}`],
    ])));

  for (const n of ov.nodes) grid.append(nodeCard(n));

  grid.append(card('Models',
    h('div', { class: 'model-grid' }, ov.models.map(modelTile)),
    h('p', { class: 'muted small' }, 'State comes from llama-swap, the orchestrator lifecycle and the media router — never from container existence alone.')));

  grid.append(card('gx-max lifecycle',
    kv([
      ['State', stateBadge(gx.state)],
      ['Phase', gx.phase || '—'],
      ['In state for', duration(gx.seconds_in_state)],
      ['Waiting requests', String(gx.waiters ?? 0)],
      ['Last measured startup', gx.last_startup_seconds ? `${gx.last_startup_seconds} s` : 'not recorded yet'],
      ['Idle release after', gx.idle_ttl ? duration(gx.idle_ttl) : '—'],
      ['Detail', gx.detail || '—'],
      ['Last error', gx.last_error ? h('span', { class: 'text-crit' }, gx.last_error) : 'none'],
    ]),
    h('a', { href: '#/jobs', class: 'btn btn-ghost btn-sm' }, 'Lifecycle events →')));

  grid.append(card('Locks & residency ledger', lockLedger(ov)));

  grid.append(card('ConnectX / RoCE rails',
    table(['Rail', 'gx10-01', 'gx10-02', 'Peer TCP', 'State'], ov.rails.map((r) => [
      `${r.name} (${r.subnet})`,
      `${r.node1.state || '—'} ${r.node1.rate || ''}`,
      `${r.node2.state || '—'} ${r.node2.rate || ''}`,
      r.tcp_probe,
      levelBadge(r.level, r.ok ? 'up' : 'down'),
    ]), { caption: 'RoCE rails' }),
    kv(ov.rails.map((r) => [`${r.name} RDMA bytes (tx/rx, node 1)`, `${bytes(r.node1.xmit_bytes)} / ${bytes(r.node1.rcv_bytes)}`]))));

  grid.append(card('Git sync', gitBlock(ov.git),
    kv([
      ['gx10-01 watcher', unitBadge(ov.git.units.node1_watcher)],
      ['gx10-01 1-min fallback', unitBadge(ov.git.units.node1_timer)],
      ['gx10-01 daily audit', unitBadge(ov.git.units.node1_daily_audit)],
      ['gx10-02 reconcile', unitBadge(ov.git.units.node2_reconcile)],
      ['gx10-02 daily audit', unitBadge(ov.git.units.node2_daily_audit)],
    ])));

  grid.append(card('Services', servicesTable(ov.services)));

  grid.append(card('Recent errors & warnings',
    ov.problems.length
      ? h('ul', { class: 'problems' }, ov.problems.map((p) => h('li', {}, levelBadge(p.level, p.level), ' ', h('strong', {}, p.source), ': ', p.message)))
      : h('p', { class: 'muted' }, 'No current warnings.')));

  clear(root).append(grid);
}

export default {
  title: 'Dashboard',
  interval: 5,
  async mount(el) {
    root = el;
  },
  async refresh({ signal }) {
    try {
      const ov = await api.get('/api/overview', { signal });
      render(ov);
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      clear(root).append(errorBox(err));
      throw err;
    }
  },
};
